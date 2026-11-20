"""Find and export highlights from local OBS recordings."""
import argparse
from array import array
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing, nullcontext
import csv
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from threading import Event


@dataclass(frozen=True)
class Recording:
    duration: float
    audio: tuple[str, ...]
    frame_size: tuple[int, int] = (1920, 1080)


@dataclass(frozen=True)
class Clip:
    start: float
    end: float
    reasons: tuple[str, ...]


@dataclass
class BatchResult:
    source: Path
    clips: list[Clip]
    exports: list[Path]
    errors: list[str]
    feed_rows: Counter = field(default_factory=Counter)
    feed_mentions: int = 0
    feed_left_mentions: int = 0
    feed_right_mentions: int = 0
    feed_unknown_mentions: int = 0
    feed_green_mentions: int | None = None
    feed_matches: int = 0
    feed_uncertain_rows: int = 0


RECORDING_EXTENSIONS = (".mkv", ".mp4", ".mov", ".ts", ".flv", ".webm")
DEFAULT_MONTAGE_WORKERS = min(4, os.cpu_count() or 1)
BACKGROUND = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, **BACKGROUND, **kwargs)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or
                           f"{command[0]} exited with status {result.returncode}")
    return result.stdout


def probe(path):
    info = json.loads(run(["ffprobe", "-v", "error", "-show_format",
                           "-show_streams", "-of", "json", str(path)]))
    video = next((s for s in info["streams"] if s["codec_type"] == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if video is None:
        raise ValueError("Select a recording with a video stream.")
    audio = tuple(s.get("tags", {}).get("title", s["codec_name"])
                  for s in info["streams"] if s["codec_type"] == "audio")
    duration = float(info["format"].get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("The recording needs a readable duration.")
    frame_size = (video["width"], video["height"])
    if any(abs(side.get("rotation", 0)) % 180 == 90 for side in video.get("side_data_list", [])):
        frame_size = frame_size[::-1]
    return Recording(duration, audio, frame_size)


def blocks(command, size):
    """Read fixed-size chunks while keeping FFmpeg's diagnostics off the data pipe."""
    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, **BACKGROUND) as process:
            try:
                while data := process.stdout.read(size):
                    yield data
                if process.wait():
                    errors.seek(0)
                    raise RuntimeError(errors.read().decode(errors="replace").strip()
                                       or "FFmpeg decoding failed.")
            finally:
                if process.poll() is None:
                    process.kill()


def decode(path, *input_options):
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-copyts", "-start_at_zero", *input_options, "-i", str(path)]


def audio_levels(path, track):
    # Measure RMS in 100 ms windows, after aligning audio to the recording clock.
    command = decode(path) + ["-map", f"0:a:{track - 1}", "-vn", "-ac", "1",
                              "-af", "aresample=8000:first_pts=0",
                              "-f", "s16le", "pipe:1"]
    for data in blocks(command, 1600):
        samples = array("h", data)
        if sys.byteorder != "little":
            samples.byteswap()
        yield sum(value * value for value in samples) / (len(samples) * 32768 ** 2)


def detect_mic_track(path, recording, progress):
    tracks = list(enumerate(recording.audio, 1))
    named = [index for index, title in tracks
             if re.fullmatch(r"(?:my |local |obs )?(?:mic|microphone)(?: input)?",
                             title.strip(), re.IGNORECASE)]
    if len(named) == 1:
        progress(f"Auto-selected mic track {named[0]} ({recording.audio[named[0] - 1]}) by name.")
        return named[0], None

    candidates = []
    for index, title in tracks:
        progress(f"Checking audio track {index}/{len(tracks)} ({title})")
        levels = list(audio_levels(path, index))
        if not levels:
            continue
        upper = sorted(levels)[int(.995 * (len(levels) - 1))]
        # Allow a low, steady mic noise floor if bursts are much louder.
        quiet_limit = max(10 ** (-48 / 10), min(10 ** (-34 / 10), upper / 100))
        quiet = sum(level <= quiet_limit for level in levels) / len(levels)
        active = sum(level >= quiet_limit * 10 ** (8 / 10) for level in levels)
        progress(f"Track {index}: {quiet:.0%} quiet, {active / len(levels):.0%} active.")
        if quiet >= .35 and active >= max(3, .005 * len(levels)):
            candidates.append((index, levels))
    if len(candidates) == 1:
        index, levels = candidates[0]
        progress(f"Auto-selected mic track {index} ({recording.audio[index - 1]}).")
        return index, levels
    detail = ("Several tracks have quiet gaps and bursts: "
              + ", ".join(str(index) for index, _ in candidates)
              if candidates else "No track had enough quiet gaps and audible bursts")
    raise ValueError(f"{detail}. Play the audio tracks to identify yours, then select it manually.")


def microphone_events(levels, threshold, duration, reason, progress, *, stop=None):
    cutoff = 10 ** (threshold / 10)
    for index, level in enumerate(levels):
        if stop is not None and stop.is_set():
            return
        if index % 300 == 0:
            progress(f"Scanning microphone at {index / 10:.0f} / {duration:.0f}s")
        if level >= cutoff:
            yield index / 10, reason


# Fractions of the full video, in width, height, left, top order.
FEED = (.42, .30, .58, .0)
SCORE = (.44, .38, .28, .34)
DEFAULT_SCORE = r"\+\s*[0-9]{2,5}\b|\b[0-9]{2,5}\s*XP\b"
NAME_TOKEN = re.compile(r"(?=[\w.-]*[^\W\d_])[\w.-]{3,32}")
GREEN_PLAYER = "Green player"
OCR_MODES = ("Grayscale", "Bright text", "Colored HUD", GREEN_PLAYER)
OCR_MODE_ARGS = dict(zip(("grayscale", "bright", "colored", "green"), OCR_MODES))
OCR_WIDTH, OCR_HEIGHT = 960, 480
OCR_HEADER = f"P5\n{OCR_WIDTH} {OCR_HEIGHT}\n255\n".encode("ascii")
OCR_CONFIDENCE = 60


@dataclass(frozen=True)
class OCRWord:
    text: str
    confidence: float
    left: int
    top: int
    width: int
    height: int
    green: bool

    @property
    def right(self):
        return self.left + self.width

    @property
    def centre(self):
        return self.left + self.width / 2

    @property
    def middle(self):
        return self.top + self.height / 2


@dataclass(frozen=True)
class OCRRow:
    words: tuple[OCRWord, ...]
    divider: float | None = None

    @property
    def text(self):
        return " ".join(word.text for word in self.words)

    @property
    def readable(self):
        return any(word.confidence >= OCR_CONFIDENCE and NAME_TOKEN.fullmatch(word.text)
                   for word in self.words)

    def side(self, word):
        if self.divider is not None:
            return "left" if word.centre < self.divider else "right"
        return "unknown"

    def name_sides(self, pattern):
        spans = []
        start = 0
        for word in self.words:
            end = start + len(word.text)
            spans.append((start, end, word))
            start = end + 1
        for match in pattern.finditer(self.text):
            hits = [word for start, end, word in spans if start < match.end() and end > match.start()]
            if hits:
                sides = {self.side(word) for word in hits}
                yield sides.pop() if len(sides) == 1 else "unknown", all(word.green for word in hits)


@dataclass(frozen=True)
class KillFeedOptions:
    region: tuple[float, float, float, float] = FEED
    any_mention: bool = False
    fallback_track: int | str = 0
    threshold: float = -18
    mode: str = OCR_MODES[0]
    divider: float | None = None


def parse_feed_region(text):
    try:
        left, top, width, height = (float(part) / 100 for part in text.split(","))
    except ValueError as error:
        raise ValueError("Enter crop percentages as left, top, width, height.") from error
    if not (0 <= left <= .99 and 0 <= top <= .99 and .01 <= width <= 1 and .01 <= height <= 1
            and left + width <= 1.0000001 and top + height <= 1.0000001):
        raise ValueError("Keep the crop within the frame and at least 1% wide and tall.")
    return width, height, left, top


def parse_feed_divider(text):
    try:
        divider = float(text) / 100
        if 0 < divider < 1:
            return divider
    except ValueError:
        pass
    raise ValueError("Enter a kill/death divider between 0 and 100%.")


def feed_divider_x(frame_size, region, divider):
    width, height = frame_size
    crop_width = max(1, int(width * region[0]))
    crop_height = max(1, int(height * region[1]))
    scaled_width = min(OCR_WIDTH, OCR_HEIGHT * crop_width / crop_height)
    return (OCR_WIDTH - scaled_width) / 2 + divider * scaled_width


def find_row_gap(words):
    names = [word for word in words if NAME_TOKEN.fullmatch(word.text) and word.confidence >= 20]
    gaps = [(right.left - left.right, (left.right + right.left) / 2)
            for left, right in zip(names, names[1:])
            if right.left - left.right >= max(5, .4 * min(left.height, right.height))]
    return max(gaps)[1] if gaps else None


def position_feed_rows(rows, fixed_divider):
    if fixed_divider is not None:
        return tuple(OCRRow(row.words, fixed_divider) for row in rows)

    # Tesseract may put the killer and victim in different text blocks.
    groups = []
    for word in sorted((word for row in rows for word in row.words), key=lambda word: -word.height):
        same_row = [row for row in groups
                    if abs(row[0].middle - word.middle) <= .5 * row[0].height
                    and min(row[0].top + row[0].height, word.top + word.height)
                    - max(row[0].top, word.top) >= .5 * word.height]
        if same_row:
            min(same_row, key=lambda row: abs(row[0].middle - word.middle)).append(word)
        else:
            groups.append([word])

    positioned = []
    for group in sorted(groups, key=lambda group: group[0].middle):
        words = tuple(sorted(group, key=lambda word: word.left))
        positioned.append(OCRRow(words, find_row_gap(words)))
    return tuple(positioned)


def ocr_filter(region, mode):
    width, height, left, top = region
    filters = (f"crop=iw*{width}:ih*{height}:iw*{left}:ih*{top}:exact=1,"
               f"scale={OCR_WIDTH}:{OCR_HEIGHT}:force_original_aspect_ratio=decrease:flags=lanczos,"
               f"pad={OCR_WIDTH}:{OCR_HEIGHT}:(ow-iw)/2:(oh-ih)/2")
    if mode in (OCR_MODES[2], GREEN_PLAYER):
        # Isolate saturated HUD lettering from relatively neutral scenery.
        ink = "if(gte(max(abs(cb(X,Y)-128),abs(cr(X,Y)-128)),22),0,255)"
        if mode == GREEN_PLAYER:
            # Use chroma differences to keep green black and other HUD colors gray.
            cb, cr = "(cb(X,Y)-128)", "(cr(X,Y)-128)"
            green = f"gte(min(-.2*{cb}-2*{cr},-2*{cb}-.5*{cr}),18)"
            ink = f"if({green},0,if(eq({ink},0),92,255))"
        filters += f",format=yuv444p,geq=lum='{ink}':cb=128:cr=128:interpolation=nearest"
    filters += ",format=gray,setsar=1"
    if mode == OCR_MODES[1]:
        filters += ",lut=y='if(gte(val,175),0,255)'"
    return filters


def has_green_ink(frame, row):
    left, top, width, height = (int(row[field]) for field in ("left", "top", "width", "height"))
    green = ink = 0
    for y in range(top, min(top + height, OCR_HEIGHT)):
        start = len(OCR_HEADER) + y * OCR_WIDTH + left
        pixels = frame[start:start + min(width, OCR_WIDTH - left)]
        green += pixels.count(0)
        ink += len(pixels) - pixels.count(255)
    return green >= 3 and green >= .25 * ink


def read_ocr_frame(frame, page_mode, environment, mode):
    text = run(["tesseract", "stdin", "stdout", "-l", "eng", "--psm", page_mode, "tsv"],
               input=frame, env=environment).decode(errors="replace")
    lines = {}
    for row in csv.DictReader(text.splitlines(), delimiter="\t", quoting=csv.QUOTE_NONE):
        word = row.get("text")
        if row.get("level") == "5" and word and word.strip():
            key = (row["page_num"], row["block_num"], row["par_num"], row["line_num"])
            left, top, width, height = (int(row[field]) for field in ("left", "top", "width", "height"))
            green = mode == GREEN_PLAYER and has_green_ink(frame, row)
            lines.setdefault(key, []).append(OCRWord(word.strip(), float(row["conf"]),
                                                     left, top, width, height, green))
    return tuple(OCRRow(tuple(words)) for words in lines.values())


def preview_feed(path, seconds, region, mode, divider=None):
    pixels = run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{seconds:.3f}",
                  "-filter_threads", "1", "-threads:v", "1", "-i", str(path), "-map", "0:V:0",
                  "-an", "-frames:v", "1", "-vf", ocr_filter(region, mode), "-threads:v", "1",
                  "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"])
    if len(pixels) != OCR_WIDTH * OCR_HEIGHT:
        raise ValueError("No preview frame at that time. Try an earlier frame.")
    image = OCR_HEADER + pixels
    rows = read_ocr_frame(image, "6", {**os.environ, "OMP_THREAD_LIMIT": "1"}, mode)
    split = feed_divider_x(probe(path).frame_size, region, divider) if divider is not None else None
    return image, position_feed_rows(rows, split), split


def ocr_events(path, region, reason, identify, duration, progress, *,
               page_mode="11", stop=None, mode=OCR_MODES[0]):
    filters = "fps=2:start_time=0," + ocr_filter(region, mode)
    command = decode(path, "-filter_threads", "1", "-threads:v", "1")
    command += ["-map", "0:V:0", "-an", "-vf", filters, "-threads:v", "1",
                "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    tesseract_env = {**os.environ, "OMP_THREAD_LIMIT": "1"}
    recent = deque(maxlen=3)
    last_frame = None
    with closing(blocks(command, OCR_WIDTH * OCR_HEIGHT)) as frames:
        for index, data in enumerate(frames):
            if stop is not None and stop.is_set():
                return
            if len(data) != OCR_WIDTH * OCR_HEIGHT:
                raise RuntimeError("FFmpeg returned a partial video frame.")
            timestamp = index / 2
            if index % 20 == 0:
                progress(f"Scanning {reason.lower()} at {timestamp:.0f} / {duration:.0f}s")
            if data != last_frame:
                rows = read_ocr_frame(OCR_HEADER + data, page_mode, tesseract_env, mode)
                last_frame = data
            matches = identify(rows)
            previous = set().union(*recent)
            if matches - previous:
                yield timestamp, reason
            recent.append(matches)


def find_clips(events, duration, before, after):
    clips = []
    for timestamp, reason in sorted(events):
        start = max(0, timestamp - before)
        end = min(duration, timestamp + after)
        if start >= end:
            continue
        if clips and start <= clips[-1].end:
            last = clips[-1]
            clips[-1] = Clip(last.start, max(last.end, end),
                             tuple(sorted(set(last.reasons) | {reason})))
        else:
            clips.append(Clip(start, end, (reason,)))
    return clips


def validate_detection(track, threshold, player, score, before, after):
    if track != "auto" and track < 0:
        raise ValueError("Choose a positive audio track number, or use 0 to disable.")
    if not math.isfinite(threshold) or not -90 <= threshold <= 0:
        raise ValueError("Mic threshold must be between -90 and 0 dBFS.")
    if not all(math.isfinite(value) and value >= 0 for value in (before, after)) or after == 0:
        raise ValueError("Lead-in must be nonnegative and follow-through must be positive.")
    player = player.strip()
    score_pattern = re.compile(score, re.IGNORECASE) if score else None
    if not (track or player or score_pattern):
        raise ValueError("Enable the mic, kill feed, or score detector.")
    if (player or score_pattern) and not shutil.which("tesseract"):
        raise RuntimeError("Install Tesseract with English language data for on-screen detection.")
    return player, score_pattern


def scan(path, recording, track, threshold, player, score, before, after, progress=print,
         *, feed=None, diagnostics=None, stop=None):
    player, score_pattern = validate_detection(track, threshold, player, score, before, after)
    levels = None
    auto = track == "auto"
    if auto:
        try:
            track, levels = detect_mic_track(path, recording, progress)
        except ValueError as error:
            if not (player or score_pattern):
                raise
            progress(f"Mic detection skipped: {error}")
            track = 0
    if track > len(recording.audio):
        raise ValueError(f"Audio track {track} is missing. Recording has {len(recording.audio)} audio track(s).")
    events = []
    if track:
        if levels is None:
            levels = audio_levels(path, track)
        reason = f"Mic peak (track {track})" if auto else "Mic peak"
        events.extend(microphone_events(levels, threshold, recording.duration, reason, progress))
    if player:
        killer_only = feed is not None and not feed.any_mention
        green_only = feed is not None and feed.mode == GREEN_PLAYER
        player_pattern = re.compile(r"(?<!\w)" + re.escape(player) + r"(?!\w)", re.IGNORECASE)
        divider = (feed_divider_x(recording.frame_size, feed.region, feed.divider)
                   if feed and feed.divider is not None else None)

        def feed_matches(rows):
            if feed:
                rows = position_feed_rows(rows, divider)
            lines = {row.text.casefold() for row in rows}
            mentions = {line for line in lines if player_pattern.search(line)}
            sides = set()
            green_name = False
            matches = set()
            if feed:
                for row in rows:
                    for side, green in row.name_sides(player_pattern):
                        sides.add(side)
                        green_name |= green
                        if (side == "left" or not killer_only) and (green or not green_only):
                            matches.add(row.text.casefold())
            else:
                matches = mentions
            if diagnostics is not None:
                diagnostics.feed_mentions += bool(mentions)
                diagnostics.feed_left_mentions += "left" in sides
                diagnostics.feed_right_mentions += "right" in sides
                diagnostics.feed_unknown_mentions += "unknown" in sides
                if green_only:
                    diagnostics.feed_green_mentions = (diagnostics.feed_green_mentions or 0) + green_name
                diagnostics.feed_matches += bool(matches)
                readable = {row.text.casefold() for row in rows if row.readable} | mentions
                diagnostics.feed_uncertain_rows += len(lines - readable)
                for line in sorted(readable, key=lambda line: line not in mentions):
                    line = line[:160]
                    if len(diagnostics.feed_rows) < 100 or line in diagnostics.feed_rows:
                        diagnostics.feed_rows[line] += 1
            return matches

        events.extend(ocr_events(path, feed.region if feed else FEED, "Kill feed", feed_matches,
                                 recording.duration, progress, page_mode="6" if feed else "11",
                                 stop=stop, mode=feed.mode if feed else OCR_MODES[0]))
    if score_pattern:
        def score_matches(rows):
            text = "\n".join(row.text for row in rows)
            return {"".join(match.group().upper().split()) for match in score_pattern.finditer(text)}

        events.extend(ocr_events(path, SCORE, "Score popup", score_matches,
                                 recording.duration, progress))
    return find_clips(events, recording.duration, before, after)


def loud_fallback(source, recording, feed, before, after, progress, stop):
    if not recording.audio or (stop is not None and stop.is_set()):
        return []
    track, levels = feed.fallback_track, None
    if track == "auto" and len(recording.audio) > 1:
        try:
            track, levels = detect_mic_track(source, recording, progress)
        except ValueError:
            track = 1
    if track == "auto" or track > len(recording.audio):
        track = 1
    progress(f"No kill match. Searching for loud audio on track {track}.")
    if levels is None:
        levels = audio_levels(source, track)
    with closing(levels) if hasattr(levels, "close") else nullcontext(levels) as samples:
        return find_clips(microphone_events(samples, feed.threshold, recording.duration,
                                            f"Loud audio (track {track})", progress, stop=stop),
                          recording.duration, before, after)


def export_clip(source, clip, directory, progress=print, *, audio_offset_ms=0):
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{source.stem}_{round(clip.start * 1000):09d}_{round(clip.end * 1000):09d}"
    with tempfile.TemporaryDirectory(prefix=".quickclip-", dir=directory) as temp:
        encoded = Path(temp) / "clip.mkv"
        # Seek near the clip, then decode a lead-in before cutting audio and video together.
        seek = max(0, clip.start - 5 - max(0, audio_offset_ms / 1000))
        audio_filter = "aresample=async=1"
        if audio_offset_ms:
            audio_filter += f",asetpts=PTS+({audio_offset_ms}/1000)/TB"
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                   "-filter_threads", "1", "-ss", f"{seek:.3f}"]
        streams = ["-i", str(source), "-ss", f"{clip.start - seek:.3f}",
                   "-t", f"{clip.end - clip.start:.3f}",
                   "-map", "0:V:0", "-map", "0:a?", "-map_chapters", "-1",
                   # Preserve the recording's frame cadence and audio timestamp gaps.
                   "-fps_mode", "passthrough", "-af", audio_filter,
                   "-c:v", "libx264", "-crf", "18", "-c:a", "flac"]
        try:
            run(command + ["-threads:v", "2"] + streams
                + ["-threads:v", "2", "-preset", "fast", "-x264-params",
                   "rc-lookahead=12:lookahead-threads=1", str(encoded)])
        except RuntimeError as error:
            if not re.search(r"malloc of size .* failed|cannot allocate memory|out of memory",
                             str(error), re.IGNORECASE):
                raise
            encoded.unlink(missing_ok=True)
            progress("Encoder ran out of memory. Retrying with a lower-memory preset.")
            run(command + ["-threads:v", "1"] + streams
                + ["-threads:v", "1", "-preset", "ultrafast", str(encoded)])
        return publish_export(encoded, directory, stem)


def publish_export(encoded, directory, stem):
    for suffix in range(10000):
        destination = directory / (stem + (f"_{suffix}" if suffix else "") + ".mkv")
        try:
            # Reserve the filename before moving the completed encode into place.
            destination.touch(exist_ok=False)
        except FileExistsError:
            continue
        try:
            encoded.replace(destination)
        except OSError:
            destination.unlink(missing_ok=True)
            raise
        return destination
    raise RuntimeError("Too many exports with the same name.")


def folder_recordings(folder):
    sources = sorted((path for path in folder.iterdir()
                      if path.suffix.lower() in RECORDING_EXTENSIONS and path.is_file()),
                     key=lambda path: path.name.casefold())
    if not sources:
        raise ValueError("No supported recordings found in the selected folder.")
    return sources


def prepare_output(sources, output):
    destination = output.resolve()
    if any(source.parent.resolve() == destination for source in sources):
        raise ValueError("Choose a separate output folder so exports are excluded from future scans.")
    output.mkdir(parents=True, exist_ok=True)


def validate_batch(options):
    validate_detection(*options)
    for program in ("ffmpeg", "ffprobe"):
        if not shutil.which(program):
            raise RuntimeError(f"Install {program} and add it to PATH.")


def scan_recording(source, options, progress, *, feed=None, stop=None):
    result = BatchResult(source, [], [], [])
    if stop is not None and stop.is_set():
        return result
    progress("Reading recording")
    try:
        recording = probe(source)
        result.clips = scan(source, recording, *options, progress=progress, feed=feed,
                            diagnostics=result if feed else None, stop=stop)
        if feed and feed.fallback_track and not result.clips:
            result.clips = loud_fallback(source, recording, feed, *options[-2:], progress, stop)
    except (OSError, RuntimeError, ValueError) as error:
        result.errors.append(str(error))
    return result


def process_batch(sources, options, output=None, progress=print, *, audio_offset_ms=0):
    validate_batch(options)
    if output is not None:
        prepare_output(sources, output)
    total = len(sources)
    for index, source in enumerate(sources, 1):
        prefix = f"[{index}/{total}] {source.name}"

        def report(message):
            progress(f"{prefix} · {message}")

        result = scan_recording(source, options, report)
        if output is not None:
            export_candidates(result, output, report, audio_offset_ms)
        yield result


def export_candidates(result, output, progress, audio_offset_ms):
    for count, clip in enumerate(result.clips, 1):
        progress(f"Exporting clip {count}/{len(result.clips)} ({clip.start:.2f}–{clip.end:.2f}s)")
        try:
            result.exports.append(export_clip(result.source, clip, output, progress=progress,
                                              audio_offset_ms=audio_offset_ms))
        except (OSError, RuntimeError) as error:
            result.errors.append(f"Clip at {clip.start:.2f}s: {error}")


def montage_layout(path):
    info = json.loads(run(["ffprobe", "-v", "error", "-show_entries",
                           "stream=codec_type,codec_name,time_base,width,height,pix_fmt,sample_aspect_ratio,"
                           "channels,channel_layout,sample_rate,sample_fmt:stream_tags=title,language",
                           "-of", "json", str(path)]))
    return info["streams"]


def compatible_montage_layout(layout, expected):
    if layout == expected[:len(layout)]:
        return True
    if len(layout) != 2 or len(expected) <= 2 or layout[0] != expected[0]:
        return False
    # A single legacy audio track occupies the main audio slot even if its title differs.
    return ({key: value for key, value in layout[1].items() if key != "tags"}
            == {key: value for key, value in expected[1].items() if key != "tags"})


def pad_montage_audio(path, present, expected):
    missing = expected[len(present):]
    if not missing:
        return
    duration = probe(path).duration
    padded = path.with_name("padded-" + path.name)
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path)]
    for stream in missing:
        layout = stream.get("channel_layout") or f'{stream["channels"]}c'
        command += ["-f", "lavfi", "-i", f'anullsrc=r={stream["sample_rate"]}:cl={layout}']
    command += ["-t", f"{duration:.3f}", "-map", "0", "-c", "copy"]
    for input_index, stream in enumerate(missing, 1):
        index = len(present) + input_index - 2
        command += ["-map", f"{input_index}:a:0", f"-c:a:{index}", "flac"]
        if stream.get("sample_fmt"):
            command += [f"-sample_fmt:a:{index}", stream["sample_fmt"]]
    run(command + [str(padded)])
    padded.replace(path)


def scan_kills(sources, player, before, after, progress=print, *,
               workers=DEFAULT_MONTAGE_WORKERS, stop=None, feed=KillFeedOptions()):
    if not player.strip():
        raise ValueError("Enter your player name to make a kill montage.")
    if workers < 1:
        raise ValueError("Choose at least one montage scan worker.")
    options = (0, feed.threshold, player, "", before, after)
    validate_batch(options)
    if not sources:
        return
    if stop is None:
        stop = Event()

    def scan_file(index, source):
        def report(message):
            progress(f"[{index}/{len(sources)}] {source.name} · {message}")

        return scan_recording(source, options, report, feed=feed, stop=stop)

    pool = ThreadPoolExecutor(max_workers=min(workers, len(sources)))
    try:
        pending = [pool.submit(scan_file, index, source) for index, source in enumerate(sources, 1)]
        for future in as_completed(pending):
            if stop.is_set():
                break
            yield future.result()
    finally:
        stop.set()
        pool.shutdown(cancel_futures=True)


def write_montage(sources, candidates, output, progress=print, *, audio_offset_ms=0):
    prepare_output(sources, output)
    source_order = {source: index for index, source in enumerate(sources)}
    with tempfile.TemporaryDirectory(prefix=".quickclip-montage-", dir=output) as temp:
        staging = Path(temp)
        results = []
        segments = []
        for result in candidates:
            results.append(result)
            if result.clips:
                progress(f"{result.source.name} · Preparing montage clips")
                export_candidates(result, staging, progress, audio_offset_ms)
            for clip_index, segment in enumerate(result.exports):
                layout = montage_layout(segment)
                name = f"segment-{len(segments):06d}.mkv"
                segment.replace(staging / name)
                segments.append((source_order[result.source], clip_index, name, layout))
        if not segments:
            return results, None
        progress(f"Joining {len(segments)} highlight segment(s)…")
        expected = max((layout for _, _, _, layout in segments), key=len)
        for source_index, _, name, layout in segments:
            if not compatible_montage_layout(layout, expected):
                raise ValueError(f"{sources[source_index].name} has a different video or audio layout. "
                                 "Build separate montages for recordings with matching settings.")
            pad_montage_audio(staging / name, layout, expected)
        manifest = staging / "segments.txt"
        manifest.write_text("".join(f"file '{name}'\n" for _, _, name, _ in sorted(segments)), encoding="ascii")
        encoded = staging / "montage.mkv"
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                   "-f", "concat", "-i", str(manifest), "-map", "0", "-c", "copy"]
        for index, stream in enumerate(expected[1:]):
            tags = stream.get("tags", {})
            for key in ("title", "language"):
                command += [f"-metadata:s:a:{index}", f"{key}={tags.get(key, '')}"]
        run(command + [str(encoded)])
        return results, publish_export(encoded, output, "kill-montage")


def kill_montage(sources, player, before, after, output, progress=print, *,
                 audio_offset_ms=0, workers=DEFAULT_MONTAGE_WORKERS, feed=KillFeedOptions()):
    with closing(scan_kills(sources, player, before, after, progress, workers=workers,
                            feed=feed)) as candidates:
        return write_montage(sources, candidates, output, progress, audio_offset_ms=audio_offset_ms)


def parse_mic_track(value):
    if value.lower() == "auto":
        return "auto"
    try:
        return int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("use auto, 0 (off), or a 1-based track number") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path, nargs="?", help="Recording or folder. Omit to open the desktop app.")
    parser.add_argument("--mic-track", type=parse_mic_track,
                        help="auto, 0 (off), or a 1-based track number. Defaults to auto without OCR.")
    parser.add_argument("--threshold", type=float, default=-18, help="Mic RMS threshold in dBFS.")
    parser.add_argument("--player", default="", help="Enable kill-feed OCR for this name.")
    parser.add_argument("--score", action="store_true", help="Enable score popup OCR.")
    parser.add_argument("--score-pattern", help="Custom score regex, also enables score OCR.")
    parser.add_argument("--before", type=float, help="Seconds before each cue (10; 3 for montage).")
    parser.add_argument("--after", type=float, help="Seconds after each cue (6; 2 for montage).")
    parser.add_argument("--output", type=Path, help="Export every candidate here. Omit to list candidates.")
    parser.add_argument("--files", type=int, metavar="N",
                        help="Process only the first N recordings in a folder, sorted by filename.")
    parser.add_argument("--kill-montage", action="store_true",
                        help="Join kill-feed matches into one MKV. Requires --player and --output.")
    parser.add_argument("--workers", type=int, default=DEFAULT_MONTAGE_WORKERS,
                        help=f"Recordings to scan at once in montage mode (default {DEFAULT_MONTAGE_WORKERS}).")
    parser.add_argument("--feed-region", default="58,0,42,30", metavar="L,T,W,H",
                        help="Montage kill-feed crop as percentages of the full frame.")
    parser.add_argument("--feed-divider", metavar="PERCENT",
                        help="Use a fixed split measured from the left edge of the crop. Defaults to per-row detection.")
    parser.add_argument("--feed-text-mode", choices=OCR_MODE_ARGS, default="grayscale",
                        help="Image treatment used on the montage kill-feed crop.")
    parser.add_argument("--any-feed-mention", action="store_true",
                        help="Include any OCR line mentioning the player in a montage, including deaths.")
    parser.add_argument("--montage-audio-fallback", action="store_true",
                        help="Find loud audio in recordings with no kill-feed matches.")
    parser.add_argument("--show-feed", action="store_true",
                        help="Print the most common OCR rows from each montage recording.")
    parser.add_argument("--audio-offset-ms", type=int, default=0,
                        help="Shift all exported audio tracks. Negative brings audio earlier; positive delays it.")
    args = parser.parse_args()
    is_folder = args.recording is not None and args.recording.is_dir()
    if args.files is not None and (args.files < 1 or not is_folder):
        parser.error("--files requires a folder and a positive whole number.")
    if args.recording is None:
        from desktop import launch
        launch()
        return
    try:
        sources = folder_recordings(args.recording) if is_folder else [args.recording]
        if args.files is not None:
            sources = sources[:args.files]
        before = args.before if args.before is not None else (3 if args.kill_montage else 10)
        after = args.after if args.after is not None else (2 if args.kill_montage else 6)
        if args.kill_montage:
            if args.output is None:
                raise ValueError("--kill-montage requires an output folder.")
            fallback_track = (args.mic_track if args.mic_track else "auto") if args.montage_audio_fallback else 0
            validate_detection(fallback_track, args.threshold, args.player, "", before, after)
            feed = KillFeedOptions(parse_feed_region(args.feed_region), args.any_feed_mention,
                                   fallback_track, args.threshold,
                                   OCR_MODE_ARGS[args.feed_text_mode],
                                   parse_feed_divider(args.feed_divider) if args.feed_divider is not None else None)
            results, montage = kill_montage(sources, args.player, before, after, args.output,
                                            audio_offset_ms=args.audio_offset_ms, workers=args.workers,
                                            feed=feed)
            for result in results:
                if args.show_feed:
                    print(f"{result.source.name}: player seen in {result.feed_mentions} sample(s), "
                          f"{result.feed_matches} matching current rule.")
                    print(f"  Left side: {result.feed_left_mentions}, right side: {result.feed_right_mentions}, "
                          f"side unknown: {result.feed_unknown_mentions} sample(s).")
                    if result.feed_green_mentions is not None:
                        print(f"  Player name marked green in {result.feed_green_mentions} sample(s).")
                    for line, count in result.feed_rows.most_common(12):
                        print(f"  {count:>4}  {line}")
                    if result.feed_uncertain_rows:
                        print(f"  Skipped {result.feed_uncertain_rows} low-confidence or non-name OCR row(s).")
                    if not any(NAME_TOKEN.search(line) for line in result.feed_rows):
                        print("  No name-like text. Check --feed-region on footage with a visible kill feed.")
                for error in result.errors:
                    print(f"{result.source.name}: {error}", file=sys.stderr)
            print(f"Montage: {montage}" if montage else "No kill montage was created.")
            if any(result.errors for result in results):
                parser.exit(1)
            return
        score = args.score_pattern or (DEFAULT_SCORE if args.score else "")
        track = args.mic_track
        if track is None:
            track = 0 if args.player.strip() or score else "auto"
        options = (track, args.threshold, args.player, score, before, after)
        if is_folder:
            failed = 0
            candidates = 0
            exported = 0
            for result in process_batch(sources, options, args.output, audio_offset_ms=args.audio_offset_ms):
                candidates += len(result.clips)
                exported += len(result.exports)
                failed += bool(result.errors)
                for clip in result.clips:
                    print(f"{result.source.name}  {clip.start:.2f}–{clip.end:.2f}s  {', '.join(clip.reasons)}")
                for path in result.exports:
                    print(path)
                for error in result.errors:
                    print(f"{result.source.name}: {error}", file=sys.stderr)
            print(f"{len(sources)} recording(s), {candidates} candidate(s), "
                  f"{exported} exported, {failed} with errors.")
            if failed:
                parser.exit(1)
            return
        info = probe(args.recording)
        for index, track in enumerate(info.audio, 1):
            print(f"Audio track {index}: {track}")
        clips = scan(args.recording, info, *options)
        for clip in clips:
            print(f"{clip.start:.2f}–{clip.end:.2f}s  {', '.join(clip.reasons)}")
            if args.output is not None:
                print(export_clip(args.recording, clip, args.output, audio_offset_ms=args.audio_offset_ms))
        print(f"{len(clips)} highlight candidate(s).")
    except (OSError, RuntimeError, ValueError, re.error) as error:
        parser.exit(1, f"QuickClip: {error}\n")


if __name__ == "__main__":
    main()
