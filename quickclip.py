"""Find and export highlights from local OBS recordings."""
import argparse
from array import array
from collections import deque
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


@dataclass(frozen=True)
class Recording:
    duration: float
    audio: tuple[str, ...]


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


RECORDING_EXTENSIONS = (".mkv", ".mp4", ".mov", ".ts", ".flv", ".webm")
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
    if not any(s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")
               for s in info["streams"]):
        raise ValueError("Select a recording with a video stream.")
    audio = tuple(s.get("tags", {}).get("title", s["codec_name"])
                  for s in info["streams"] if s["codec_type"] == "audio")
    duration = float(info["format"].get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("The recording needs a readable duration.")
    return Recording(duration, audio)


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


def decode(path):
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-copyts", "-start_at_zero", "-i", str(path)]


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


def microphone_events(levels, threshold, duration, reason, progress):
    cutoff = 10 ** (threshold / 10)
    for index, level in enumerate(levels):
        if index % 300 == 0:
            progress(f"Scanning microphone at {index / 10:.0f} / {duration:.0f}s")
        if level >= cutoff:
            yield index / 10, reason


# Fractions of the full video, in width, height, left, top order.
FEED = (.42, .30, .58, .0)
SCORE = (.44, .38, .28, .34)
DEFAULT_SCORE = r"\+\s*[0-9]{2,5}\b|\b[0-9]{2,5}\s*XP\b"


def ocr_events(path, region, reason, identify, duration, progress, *, page_mode="11"):
    width, height, left, top = region
    filters = (f"fps=2:start_time=0,crop=iw*{width}:ih*{height}:iw*{left}:ih*{top},"
               "scale=768:384,format=gray")
    command = decode(path) + ["-map", "0:V:0", "-an", "-vf", filters,
                              "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    header = b"P5\n768 384\n255\n"
    recent = deque(maxlen=3)
    for index, data in enumerate(blocks(command, 768 * 384)):
        if len(data) != 768 * 384:
            raise RuntimeError("FFmpeg returned a partial video frame.")
        timestamp = index / 2
        if index % 20 == 0:
            progress(f"Scanning {reason.lower()} at {timestamp:.0f} / {duration:.0f}s")
        text = run(["tesseract", "stdin", "stdout", "-l", "eng", "--psm", page_mode],
                   input=header + data).decode(errors="replace")
        matches = identify(text)
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
         *, killer_only=False):
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
        player_pattern = re.compile(r"(?<!\w)" + re.escape(player) + r"(?!\w)", re.IGNORECASE)
        killer_pattern = re.compile(r"^\s*(?:\[[^\]]+\]\s*)?" + player_pattern.pattern,
                                    re.IGNORECASE)

        def feed_matches(text):
            if killer_only:
                return {" ".join(line.casefold().split()) for line in text.splitlines()
                        if (match := killer_pattern.match(line)) and re.search(r"\w", line[match.end():])}
            return {" ".join(line.casefold().split()) for line in text.splitlines()
                    if player_pattern.search(line)}

        events.extend(ocr_events(path, FEED, "Kill feed", feed_matches,
                                 recording.duration, progress, page_mode="6" if killer_only else "11"))
    if score_pattern:
        def score_matches(text):
            return {"".join(match.group().upper().split()) for match in score_pattern.finditer(text)}

        events.extend(ocr_events(path, SCORE, "Score popup", score_matches,
                                 recording.duration, progress))
    return find_clips(events, recording.duration, before, after)


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


def process_batch(sources, options, output=None, progress=print, *, audio_offset_ms=0,
                  killer_only=False):
    validate_detection(*options)
    for program in ("ffmpeg", "ffprobe"):
        if not shutil.which(program):
            raise RuntimeError(f"Install {program} and add it to PATH.")
    if output is not None:
        prepare_output(sources, output)

    total = len(sources)
    for index, source in enumerate(sources, 1):
        prefix = f"[{index}/{total}] {source.name}"

        def report(message):
            progress(f"{prefix} · {message}")

        report("Reading recording")
        result = BatchResult(source, [], [], [])
        try:
            result.clips = scan(source, probe(source), *options, progress=report,
                                killer_only=killer_only)
        except (OSError, RuntimeError, ValueError) as error:
            result.errors.append(str(error))
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
                           "channels,channel_layout,sample_rate:stream_tags=title,language",
                           "-of", "json", str(path)]))
    return info["streams"]


def scan_kills(sources, player, before, after, progress=print):
    if not player.strip():
        raise ValueError("Enter your player name to make a kill montage.")
    return process_batch(sources, (0, -18, player, "", before, after), progress=progress,
                         killer_only=True)


def write_montage(sources, candidates, output, progress=print, *, audio_offset_ms=0):
    prepare_output(sources, output)
    with tempfile.TemporaryDirectory(prefix=".quickclip-montage-", dir=output) as temp:
        staging = Path(temp)
        results = []
        segments = []
        layout = None
        for result in candidates:
            results.append(result)
            if result.clips:
                progress(f"{result.source.name} · Preparing montage clips")
                export_candidates(result, staging, progress, audio_offset_ms)
            for segment in result.exports:
                current = montage_layout(segment)
                if layout is not None and current != layout:
                    raise ValueError(f"{result.source.name} has a different video or audio track layout. "
                                     "Build separate montages for recordings with matching settings.")
                layout = current
                name = f"segment-{len(segments):06d}.mkv"
                segment.replace(staging / name)
                segments.append(f"file '{name}'\n")
        if not segments:
            return results, None
        progress(f"Joining {len(segments)} highlight segment(s)…")
        manifest = staging / "segments.txt"
        manifest.write_text("".join(segments), encoding="ascii")
        encoded = staging / "montage.mkv"
        run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-i", str(manifest), "-map", "0", "-c", "copy", str(encoded)])
        return results, publish_export(encoded, output, "kill-montage")


def kill_montage(sources, player, before, after, output, progress=print, *, audio_offset_ms=0):
    return write_montage(sources, scan_kills(sources, player, before, after, progress),
                         output, progress, audio_offset_ms=audio_offset_ms)


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
            results, montage = kill_montage(sources, args.player, before, after, args.output,
                                            audio_offset_ms=args.audio_offset_ms)
            for result in results:
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
