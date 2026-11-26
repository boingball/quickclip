"""Find and export highlights from local OBS recordings."""
import argparse
from array import array
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing, contextmanager, nullcontext
import csv
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from difflib import SequenceMatcher
from functools import lru_cache
import hashlib
from itertools import islice
from random import Random
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
    # (time in the recording, reason) for each detection inside the clip.
    cues: tuple[tuple[float, str], ...] = ()


@dataclass(frozen=True)
class VideoAcceleration:
    encode: bool = False
    decode: bool = False


@dataclass
class BatchResult:
    source: Path
    clips: list[Clip]
    exports: list[Path]
    errors: list[str]
    audio_tracks: int | None = None
    feed_scanned: bool = False
    feed_rows: Counter = field(default_factory=Counter)
    feed_mentions: int = 0
    feed_left_mentions: int = 0
    feed_right_mentions: int = 0
    feed_unknown_mentions: int = 0
    feed_green_mentions: int | None = None
    feed_lookalikes: Counter = field(default_factory=Counter)
    feed_matches: int = 0
    badge_samples: int | None = None
    badge_score_samples: int = 0
    badge_reference_loaded: bool = False
    badge_reference_matches: int = 0
    badge_feed_matches: int | None = None
    feed_uncertain_rows: int = 0


RECORDING_EXTENSIONS = (".mkv", ".mp4", ".mov", ".ts", ".flv", ".webm")
DEFAULT_MONTAGE_WORKERS = min(4, os.cpu_count() or 1)
BACKGROUND = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
RADEON_DECODE_ARGS = ["-xerror", "-init_hw_device", "d3d11va=radeon:,vendor_id=0x1002",
                      "-hwaccel", "d3d11va", "-hwaccel_device", "radeon"]
MONTAGE_CHECKPOINTS = ".quickclip-montage-resume"


class MontagePaused(Exception):
    pass


def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, **BACKGROUND, **kwargs)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or
                           f"{command[0]} exited with status {result.returncode}")
    return result.stdout


def run_export(command, description, progress, duration=None):
    """Report ffmpeg video output as it is written and retain stderr for failures."""
    command = command[:1] + ["-nostats", "-stats_period", "1", "-progress", "pipe:1"] + command[1:]
    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, encoding="utf-8",
                              errors="replace", **BACKGROUND) as process:
            stats = {}
            seconds = 0.0
            try:
                for line in process.stdout:
                    key, separator, value = line.strip().partition("=")
                    if not separator:
                        continue
                    stats[key] = value.strip()
                    if key != "progress":
                        continue
                    frame = stats.get("frame", "")
                    if frame.isdecimal() and int(frame) > 0:
                        details = [description]
                        timestamp = stats.get("out_time_us", "")
                        if timestamp.lstrip("-").isdecimal():
                            seconds = max(seconds, int(timestamp) / 1_000_000)
                            if duration:
                                details.append(f"{min(99, int(100 * seconds / duration))}%")
                            span = f" / {duration:.1f}s" if duration is not None else ""
                            details.append(f"{seconds:.1f}s{span} written")
                        details.append(f"{frame} frames")
                        speed = stats.get("speed", "")
                        if speed.endswith("x"):
                            details.append(speed)
                        progress(" · ".join(details))
                    stats.clear()
                if process.wait():
                    errors.seek(0)
                    raise RuntimeError(errors.read().decode(errors="replace").strip()
                                       or "FFmpeg export failed.")
            finally:
                if process.poll() is None:
                    process.kill()


@lru_cache(maxsize=8)
def ffmpeg_features(executable, listing):
    return run([executable, "-hide_banner", listing]).decode(errors="replace").split()


def resolve_video_acceleration(requested, progress=print):
    if not (requested.encode or requested.decode):
        return requested
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise RuntimeError("Install ffmpeg and add it to PATH.")
    encode = requested.encode and "h264_amf" in ffmpeg_features(executable, "-encoders")
    decode = requested.decode and "d3d11va" in ffmpeg_features(executable, "-hwaccels")
    if requested.encode and not encode:
        progress("This FFmpeg build lacks h264_amf. Using CPU encoding.")
    if requested.decode and not decode:
        progress("This FFmpeg build lacks D3D11VA. Using CPU decoding.")
    return VideoAcceleration(encode, decode)


def last_packet_time(path):
    try:
        listing = run(["ffprobe", "-v", "quiet", "-select_streams", "V:0", "-show_entries",
                       "packet=pts_time,duration_time", "-of", "csv=p=0", str(path)])
    except (OSError, RuntimeError):
        return 0
    end = 0
    for line in listing.decode(errors="replace").splitlines():
        parts = line.strip().rstrip(",").split(",")
        try:
            end = max(end, float(parts[0]) + (float(parts[1]) if len(parts) > 1 and parts[1] else 0))
        except ValueError:
            continue
    return end


def probe(path):
    info = json.loads(run(["ffprobe", "-v", "error", "-show_format",
                           "-show_streams", "-of", "json", str(path)]))
    video = next((s for s in info["streams"] if s["codec_type"] == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if video is None:
        raise ValueError("Select a recording with a video stream.")
    audio = tuple(s.get("tags", {}).get("title", s["codec_name"])
                  for s in info["streams"] if s["codec_type"] == "audio")
    try:
        duration = float(info["format"].get("duration", 0))
    except ValueError:
        duration = 0
    if not math.isfinite(duration) or duration <= 0:
        # Recordings cut off mid-write (a crash, a full disk, an interrupted replay save)
        # have no duration or seek index. Use the last video packet instead.
        duration = last_packet_time(path)
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


def video_blocks(command, size, hardware_decode, progress):
    completed = 0
    if hardware_decode:
        progress("Decoding video with Radeon D3D11VA…")
        try:
            with closing(blocks(command[:1] + RADEON_DECODE_ARGS + command[1:], size)) as frames:
                for data in frames:
                    if len(data) != size:
                        raise RuntimeError("Radeon decoder returned a partial frame.")
                    completed += 1
                    yield data
            return
        except RuntimeError:
            progress("Radeon decoding failed. Continuing with CPU decoding.")
    with closing(blocks(command, size)) as frames:
        yield from islice(frames, completed, None)



def decode(path, *input_options):
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-copyts", "-start_at_zero", *input_options, "-i", str(path)]


PREVIEW_HEIGHT = 720
HDR_TRANSFERS = ("arib-std-b67", "smpte2084")


def preview_command(path, start, duration, hardware_decode=False):
    """FFmpeg command that streams a 720p SDR copy of part of a recording for FFplay.

    FFplay alone opens 4K captures at full size and cannot use the Radeon decoder, so a 4K
    AV1 replay can overflow the screen or stall. FFmpeg decodes (on the GPU when asked; it
    falls back to the CPU by itself), shrinks the picture and converts HDR to SDR first.
    """
    try:
        transfer = run(["ffprobe", "-v", "error", "-select_streams", "V:0", "-show_entries",
                        "stream=color_transfer", "-of", "csv=p=0", str(path)]).decode().strip()
    except (OSError, RuntimeError):
        transfer = ""
    picture = f"scale=-2:'min({PREVIEW_HEIGHT},ih)':flags=bicubic"
    if transfer in HDR_TRANSFERS and "zscale" in ffmpeg_features("ffmpeg", "-filters"):
        picture += (",zscale=t=linear:npl=203,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
                    "zscale=t=bt709:m=bt709:r=tv")
    picture += ",format=yuv420p"
    hardware = ["-hwaccel", "d3d11va"] if hardware_decode else []
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *hardware,
            "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(path),
            "-map", "0:V:0", "-map", "0:a:0?", "-vf", picture,
            "-c:v", "rawvideo", "-c:a", "pcm_s16le", "-ac", "2", "-f", "nut", "pipe:1"]


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
KILL_BADGE_REGION = (.48, .20, .16, .60)
KILL_AWARD_REGION = (.34, .15, .19, .53)
KILL_BADGE_PATTERN = re.compile(r"\bKILL\b(?!\s+ASSIST\b)", re.IGNORECASE)
DEFAULT_SCORE = r"\+\s*[0-9]{2,5}\b|\b[0-9]{2,5}\s*XP\b"
NAME_TOKEN = re.compile(r"(?=[\w.-]*[^\W\d_])[\w.-]{3,32}")
OCR_LOOKALIKES = ("o0", "il1", "s5", "b8", "z2")
GREEN_PLAYER = "Green player"
FAINT_TEXT = "Faint text"
LOCAL_CONTRAST = "Local contrast"
OCR_MODES = ("Grayscale", "Bright text", "Colored HUD", GREEN_PLAYER, FAINT_TEXT, LOCAL_CONTRAST)
KILL_BADGE_MODE = OCR_MODES[0]
KILL_BADGE_PAGE_MODE = "11"
KILL_BADGE_TEXT_LUT = bytes(0 if value >= 175 else 255 for value in range(256))
OCR_PATCH_SIZE = 12
OCR_MODE_ARGS = dict(zip(("grayscale", "bright", "colored", "green", "faint", "local"), OCR_MODES))
# Half-second samples a player's kill-feed line count is remembered, to ride out OCR dropouts.
FEED_KILL_MEMORY = 6
# A kill line stays in the feed for about 10 s. Remember readable victims a little longer so
# a line read again later is not taken for a new kill.
FEED_VICTIM_MEMORY = 24
FEED_VICTIM_CONFIDENCE = 30
# How long a kill reported without a readable victim can still be matched to one.
FEED_UNNAMED_MEMORY = 12
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
    edge: float | None = None

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
        if self.edge is not None:
            # Kill-feed lines end at the same right edge. A killer's name is followed by
            # the weapon icon and the victim, so it ends well short of that edge even when
            # the victim's name is too faint to read.
            return "left" if word.right < self.edge - 2.5 * word.height else "right"
        return "unknown"

    def name_sides(self, pattern):
        for spelling, side, green, _ in self.name_words(pattern):
            yield spelling, side, green

    def name_words(self, pattern):
        """Yield each match's spelling, side, whether it is green, and its last OCR word."""
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
                side = sides.pop() if len(sides) == 1 else "unknown"
                yield match.group(), side, all(word.green for word in hits), hits[-1]


@dataclass(frozen=True)
class SkullReference:
    box: tuple[float, float, float, float]
    pixels: tuple[int, ...]

    def __post_init__(self):
        if (len(self.box) != 4 or not all(isinstance(value, (int, float)) and math.isfinite(value)
                                          for value in self.box)
                or not (-3.1 <= self.box[0] <= 0 and -.6 <= self.box[1] <= 1.6)
                or not all(.3 <= size <= 2.5 for size in self.box[2:])
                or len(self.pixels) != OCR_PATCH_SIZE ** 2
                or not all(type(value) is int and 0 <= value <= 255 for value in self.pixels)):
            raise ValueError("Invalid skull reference.")


@dataclass(frozen=True)
class WeaponReference:
    label: str
    mode: str
    box: tuple[float, float, float, float]
    pixels: tuple[int, ...]

    def __post_init__(self):
        if (not isinstance(self.label, str) or not self.label.strip() or len(self.label) > 32
                or self.mode not in OCR_MODES or len(self.box) != 4
                or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in self.box)
                or not all(.2 <= size <= 12 for size in self.box[2:])
                or len(self.pixels) != OCR_PATCH_SIZE ** 2
                or not all(type(value) is int and 0 <= value <= 255 for value in self.pixels)):
            raise ValueError("Invalid weapon reference.")


@dataclass(frozen=True)
class KillFeedOptions:
    region: tuple[float, float, float, float] = FEED
    any_mention: bool = False
    fallback_track: int | str = 0
    threshold: float = -18
    mode: str = OCR_MODES[0]
    divider: float | None = None
    badge: bool = False
    skull_reference: SkullReference | None = None
    weapon_references: tuple[WeaponReference, ...] = ()
    badge_feed_details: bool = False


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

    edge = max((word.right for group in groups for word in group), default=None)
    positioned = []
    for group in sorted(groups, key=lambda group: group[0].middle):
        words = tuple(sorted(group, key=lambda word: word.left))
        positioned.append(OCRRow(words, find_row_gap(words), edge))
    return tuple(positioned)


def player_name_pattern(player, allow_lookalikes=False):
    exact = re.escape(player)
    alternatives = [exact]
    if allow_lookalikes and len(player) >= 6 and player.isascii() and player.isalnum():
        for index, char in enumerate(player.lower()):
            for group in OCR_LOOKALIKES:
                if char in group:
                    alternatives.append(exact[:index] + f"[{group}]" + exact[index + 1:])
                    break
    return re.compile(r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)", re.IGNORECASE)


def ocr_filter(region, mode):
    width, height, left, top = region
    # Convert to 8 bits before cropping. With 10-bit video (such as HDR game captures),
    # FFmpeg 8 garbles letter edges when an exact crop at an odd offset is converted
    # later, which defeats OCR.
    convert = "format=gray" if mode in (OCR_MODES[0], OCR_MODES[1], LOCAL_CONTRAST) else "format=yuv444p"
    filters = (f"{convert},crop=iw*{width}:ih*{height}:iw*{left}:ih*{top}:exact=1,"
               f"scale={OCR_WIDTH}:{OCR_HEIGHT}:force_original_aspect_ratio=decrease:flags=lanczos")
    pad = f"pad={OCR_WIDTH}:{OCR_HEIGHT}:(ow-iw)/2:(oh-ih)/2"
    if mode == FAINT_TEXT:
        # Compare luminance and chroma with the local background before padding.
        # Absolute differences preserve pale and dark strokes in the same row.
        return (filters + ",format=yuv444p,split=2[detail][base];[base]gblur=sigma=9[background];"
                "[detail][background]blend=all_mode=difference,"
                "geq=lum='clip(255-22*max(max(max(lum(X,Y),cb(X,Y)),cr(X,Y))-1,0),0,255)':"
                "cb=128:cr=128:interpolation=nearest,format=gray," + pad + ":color=white,setsar=1")
    if mode == LOCAL_CONTRAST:
        # Stretch each pixel between the darkest and brightest values within ~10 px.
        # Translucent HUD text over bright sky differs from it by only ~15 levels, and
        # mostly in its outline, so a global threshold loses it.
        erode, dilate = ",".join(["erosion"] * 10), ",".join(["dilation"] * 10)
        return (filters + f",format=gray,split=3[y][low][high];[low]{erode},gblur=sigma=3,split=2[min1][min2];"
                f"[high]{dilate},gblur=sigma=3[max];[y][min1]blend=all_expr='A-B'[detail];"
                "[max][min2]blend=all_expr='A-B'[range];"
                "[detail][range]blend=all_expr='clip(255*A/max(B,24),0,255)',"
                + pad + ":color=white,setsar=1")
    filters += "," + pad
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


def preview_ocr_frame(path, seconds, region, mode, page_mode, *, for_ocr=None):
    pixels = run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{seconds:.3f}",
                  "-filter_threads", "1", "-threads:v", "1", "-i", str(path), "-map", "0:V:0",
                  "-an", "-frames:v", "1", "-vf", ocr_filter(region, mode), "-threads:v", "1",
                  "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"])
    if len(pixels) != OCR_WIDTH * OCR_HEIGHT:
        raise ValueError("No preview frame at that time. Try an earlier frame.")
    image = OCR_HEADER + pixels
    ocr_image = for_ocr(image) if for_ocr else image
    rows = read_ocr_frame(ocr_image, page_mode, {**os.environ, "OMP_THREAD_LIMIT": "1"}, mode)
    return image, rows


def preview_feed(path, seconds, region, mode, divider=None):
    image, rows = preview_ocr_frame(path, seconds, region, mode, "6")
    split = feed_divider_x(probe(path).frame_size, region, divider) if divider is not None else None
    return image, position_feed_rows(rows, split), split


def preview_kill_badge(path, seconds):
    image, rows = preview_ocr_frame(path, seconds, KILL_BADGE_REGION, KILL_BADGE_MODE,
                                    KILL_BADGE_PAGE_MODE, for_ocr=badge_text_frame)
    return image, rows, None


def badge_text_frame(frame):
    return OCR_HEADER + frame[len(OCR_HEADER):].translate(KILL_BADGE_TEXT_LUT)


def connected_pixels(points):
    pixels = set(points)
    while pixels:
        stack = [pixels.pop()]
        component = set(stack)
        while stack:
            x, y = stack.pop()
            for point in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if point in pixels:
                    pixels.remove(point)
                    component.add(point)
                    stack.append(point)
        yield component


def skull_shape(component, score_height):
    left = min(x for x, _ in component)
    right = max(x for x, _ in component) + 1
    top = min(y for _, y in component)
    bottom = max(y for _, y in component) + 1
    width, height = right - left, bottom - top
    if not (.55 * score_height <= height <= 1.6 * score_height
            and .55 * height <= width <= 1.35 * height):
        return False
    jaw = {x for x, y in component if y >= bottom - max(1, height // 5)}
    if max(jaw) - min(jaw) + 1 > .85 * width:
        return False

    # Two interior openings on opposite sides distinguish a skull from a generic score icon.
    background = ((x, y) for y in range(top, bottom) for x in range(left, right)
                  if (x, y) not in component)
    eyes = []
    for hole in connected_pixels(background):
        if len(hole) < max(2, width * height * .012):
            continue
        if any(x in (left, right - 1) or y in (top, bottom - 1) for x, y in hole):
            continue
        x = sum(x for x, _ in hole) / len(hole)
        y = sum(y for _, y in hole) / len(hole)
        if top + .2 * height <= y <= top + .73 * height:
            eyes.append((x, y))
    middle = left + width / 2
    return any(x1 < middle < x2 and x2 - x1 >= .2 * width and abs(y1 - y2) <= .2 * height
               for x1, y1 in eyes for x2, y2 in eyes)


def skull_search_area(score):
    height = score.height
    left = max(0, score.left - round(3 * height))
    right = max(left, score.left - max(1, round(.15 * height)))
    top = max(0, score.top - height // 2)
    bottom = min(OCR_HEIGHT, score.top + round(1.55 * height))
    return left, top, right, bottom


def skull_search_preview(frame, score):
    left, top, right, bottom = skull_search_area(score)
    if right <= left or bottom <= top:
        return None
    header = f"P5\n{right - left} {bottom - top}\n255\n".encode("ascii")
    rows = []
    for y in range(top, bottom):
        offset = len(OCR_HEADER) + y * OCR_WIDTH
        rows.append(frame[offset + left:offset + right])
    return header + b"".join(rows)


def sample_ocr_patch(frame, left, top, width, height):
    if left < 0 or top < 0 or left + width > OCR_WIDTH or top + height > OCR_HEIGHT:
        return ()
    columns = [left + width * (2 * x + 1) // (2 * OCR_PATCH_SIZE)
               for x in range(OCR_PATCH_SIZE)]
    offsets = [len(OCR_HEADER) + OCR_WIDTH * (top + height * (2 * y + 1) // (2 * OCR_PATCH_SIZE))
               for y in range(OCR_PATCH_SIZE)]
    return tuple(frame[offset + x] for offset in offsets for x in columns)


def normalize_ocr_patch(pixels):
    if not pixels:
        return ()
    mean = sum(pixels) / len(pixels)
    centered = [pixel - mean for pixel in pixels]
    length = math.sqrt(sum(pixel ** 2 for pixel in centered))
    if length < 5 * math.sqrt(len(pixels)):
        return ()
    return tuple(pixel / length for pixel in centered)


def weapon_anchor(row):
    if row.divider is None:
        return None
    names = [word for word in row.words if NAME_TOKEN.fullmatch(word.text)]
    left = [word for word in names if word.centre < row.divider]
    right = [word for word in names if word.centre >= row.divider]
    if not left or not right:
        return None
    a, b = left[-1], right[0]
    return a.right, b.left, (a.middle + b.middle) / 2, max(a.height, b.height)


def make_weapon_reference(frame, rows, bounds, label, mode):
    left, top, right, bottom = bounds
    width, height = right - left, bottom - top
    for row in rows:
        anchor = weapon_anchor(row)
        if anchor is None:
            # Without a readable victim, anchor on the nearest name to the left of the icon.
            killers = [word for word in row.words if NAME_TOKEN.fullmatch(word.text) and word.right <= left + 2
                       and left - word.right <= 2 * word.height]
            if not killers:
                continue
            killer = max(killers, key=lambda word: word.right)
            anchor = (killer.right, killer.right + WEAPON_SEARCH_HEIGHTS * killer.height,
                      killer.middle, killer.height)
        start, end, middle, row_height = anchor
        if not (start - 2 <= left < right <= end + 2
                and abs((top + bottom) / 2 - middle) <= .75 * row_height):
            continue
        pixels = sample_ocr_patch(frame, left, top, width, height)
        if normalize_ocr_patch(pixels):
            box = ((left - (start + end) / 2) / row_height, (top - middle) / row_height,
                   width / row_height, height / row_height)
            return WeaponReference(label.strip(), mode, box, pixels)
    raise ValueError("Select a clear weapon icon just after a readable name on one kill-feed row.")


def killer_anchor(row, killer):
    """Search span for the weapon icon after the killer's name, even when the victim is unreadable."""
    following = [word for word in row.words if word.left >= killer.right + killer.height
                 and NAME_TOKEN.fullmatch(word.text) and word.confidence >= 40]
    stop = following[0].left if following else killer.right + WEAPON_SEARCH_HEIGHTS * killer.height
    return killer.right, stop, killer.middle, killer.height


def recognize_weapon(frame, row, references, mode, killer=None):
    """Return the label of the saved weapon icon found on the row, if exactly one matches well.

    Each icon is searched for across the gap after the killer's name, so it is found
    wherever the feed places it and without needing the victim's name.
    """
    anchor = killer_anchor(row, killer) if killer is not None else weapon_anchor(row)
    if anchor is None:
        names = [word for word in row.words if NAME_TOKEN.fullmatch(word.text)]
        if not names:
            return None
        anchor = killer_anchor(row, names[0])
    start, end, middle, height = anchor
    scores = []
    for reference in references:
        if reference.mode != mode:
            continue
        template = normalize_ocr_patch(reference.pixels)
        _, y, width, box_height = (round(value * height) for value in reference.box)
        if not template or width < 2 or box_height < 2:
            continue
        y += round(middle)
        step = max(1, height // 6)
        best = 0
        for x in range(round(start) - 2, round(end) + 3 - width, step):
            for dy in (0, -step, step):
                patch = normalize_ocr_patch(sample_ocr_patch(frame, x, y + dy, width, box_height))
                best = max(best, sum(a * b for a, b in zip(template, patch)))
        scores.append((best, reference.label))
    scores.sort(reverse=True)
    if (scores and scores[0][0] >= WEAPON_MATCH
            and (len(scores) == 1 or scores[0][0] - scores[1][0] >= .04 or scores[0][1] == scores[1][1])):
        return scores[0][1]
    return None


def badge_victim_region(frame_size, score):
    width, height = frame_size
    badge_width, badge_height, badge_left, badge_top = KILL_BADGE_REGION
    crop_width = max(1, int(width * badge_width))
    crop_height = max(1, int(height * badge_height))
    scale = min(OCR_WIDTH / crop_width, OCR_HEIGHT / crop_height)
    pad_x = (OCR_WIDTH - crop_width * scale) / 2
    pad_y = (OCR_HEIGHT - crop_height * scale) / 2
    left = max(badge_left, badge_left + (score.left - 9 * score.height - pad_x) / (width * scale))
    right = min(badge_left + badge_width, badge_left + (score.right + score.height - pad_x) / (width * scale))
    top = max(badge_top, badge_top + (score.top + score.height - pad_y) / (height * scale))
    bottom = min(badge_top + badge_height,
                 badge_top + (score.top + 3.2 * score.height - pad_y) / (height * scale))
    if right - left >= .01 and bottom - top >= .01:
        return right - left, bottom - top, left, top
    return None


def feed_row_names(row):
    if weapon_anchor(row) is None:
        return (), ()
    names = [word for word in row.words if NAME_TOKEN.fullmatch(word.text)]
    return (tuple(word.text for word in names if row.side(word) == "left"),
            tuple(word.text for word in names if row.side(word) == "right"))


def badge_feed_details(path, recording, timestamp, badge_rows, player, feed, stop):
    if stop is not None and stop.is_set():
        return ()
    scores = badge_scores(badge_rows)
    victims = set()
    if scores:
        score = max(scores, key=lambda word: word.confidence)
        region = badge_victim_region(recording.frame_size, score)
        if region:
            _, rows = preview_ocr_frame(path, timestamp, region, OCR_MODES[2], "11")
            hud_words = {"assist", "destroyed", "equipment", "vehicle", "helicopter",
                         "offensive", "defensive", "squad", "killstreak"}
            victims = {word.text.casefold() for row in rows for word in row.words
                       if word.confidence >= 55 and len(word.text) >= 5
                       and NAME_TOKEN.fullmatch(word.text) and word.text.casefold() not in hud_words}
    if not (player or victims):
        return ()

    green_only = feed.mode == GREEN_PLAYER
    player_pattern = player_name_pattern(player, allow_lookalikes=True) if player else None
    divider = (feed_divider_x(recording.frame_size, feed.region, feed.divider)
               if feed.divider is not None else None)

    def read_feed(seconds):
        frame, rows = preview_ocr_frame(path, seconds, feed.region, feed.mode, "6")
        return frame, position_feed_rows(rows, divider)

    baseline = None
    matches = {}
    for seconds in (timestamp, timestamp + .35, timestamp - .35):
        if stop is not None and stop.is_set():
            return ()
        if not 0 <= seconds < recording.duration:
            continue
        frame, rows = read_feed(seconds)
        for row in rows:
            left, right = feed_row_names(row)
            if not (left and right):
                continue
            identity = tuple(" ".join(part).casefold() for part in (left, right))
            is_player = player_pattern is not None and any(
                side == "left" and (green or not green_only)
                for _, side, green in row.name_sides(player_pattern)
            )
            is_victim = any(name.casefold() in victims for name in right)
            if not is_victim:
                if not is_player or timestamp < 1:
                    continue
                if baseline is None:
                    _, earlier = read_feed(timestamp - 1)
                    baseline = {tuple(" ".join(part).casefold() for part in feed_row_names(row))
                                for row in earlier}
                if identity in baseline:
                    continue
            confidence = 2 * is_victim + is_player
            weapon = recognize_weapon(frame, row, feed.weapon_references, feed.mode)
            if identity not in matches:
                matches[identity] = [confidence, " ".join(right), set()]
            match = matches[identity]
            match[0] = max(match[0], confidence)
            if weapon:
                match[2].add(weapon)

    if not matches:
        return ()
    highest = max(match[0] for match in matches.values())
    winners = [match for match in matches.values() if match[0] == highest]
    if len(winners) != 1:
        return ()
    _, victim, weapons = winners[0]
    if len(weapons) == 1:
        return f"Victim: {victim}", f"Weapon: {next(iter(weapons))}"
    return (f"Victim: {victim}",)


def make_skull_reference(frame, score, bounds):
    left, top, right, bottom = bounds
    search_left, search_top, search_right, search_bottom = skull_search_area(score)
    width, height = right - left, bottom - top
    if not (search_left <= left < right <= search_right and search_top <= top < bottom <= search_bottom
            and width >= .3 * score.height and height >= .3 * score.height):
        raise ValueError("Draw a tight box around the whole skull inside the zoomed area.")
    pixels = sample_ocr_patch(frame, left, top, width, height)
    if not normalize_ocr_patch(pixels):
        raise ValueError("That selection has too little contrast. Include the outline and eye sockets.")
    box = tuple(value / score.height for value in (left - score.left, top - score.top, width, height))
    return SkullReference(box, pixels)


def matches_skull_reference(frame, score, reference):
    template = normalize_ocr_patch(reference.pixels)
    if not template:
        return False
    x, y, width, height = (round(value * score.height) for value in reference.box)
    step = max(1, score.height // 12)
    shifts = (0, -step, step, -2 * step, 2 * step)
    for scale in (1, .9, 1.1):
        w, h = max(1, round(width * scale)), max(1, round(height * scale))
        left = score.left + x + (width - w) // 2
        top = score.top + y + (height - h) // 2
        for dx in shifts:
            for dy in shifts:
                pixels = normalize_ocr_patch(sample_ocr_patch(frame, left + dx, top + dy, w, h))
                if sum(expected * actual for expected, actual in zip(template, pixels)) >= .87:
                    return True
    return False


def skull_next_to_score(frame, score):
    left, top, right, bottom = skull_search_area(score)
    samples = [
        (x, y, frame[len(OCR_HEADER) + y * OCR_WIDTH + x])
        for y in range(top, bottom) for x in range(left, right)
    ]
    previous = None
    # Lower the cutoff only in the small area to the left of the score.
    for cutoff in (175, 165, 155, 145, 135, 125, 115):
        ink = {(x, y) for x, y, value in samples if value >= cutoff}
        if ink != previous and any(skull_shape(part, score.height) for part in connected_pixels(ink)):
            return True
        previous = ink
    return False


def badge_scores(rows):
    return [word for row in rows for word in row.words
            if word.confidence >= 70 and 8 <= word.height <= OCR_HEIGHT // 6
            and re.fullmatch(r"[0-9]{2,4}", word.text)]


def kill_awards(rows):
    awards = set()
    for row in rows:
        text = row.text.upper()
        awards.update(re.findall(r"\b(?:BALLISTIC|FLYING ACE)\b", text))
        for index, word in enumerate(row.words[:-1]):
            pair = row.words[index:index + 2]
            if " ".join(part.text.upper() for part in pair) != "NEW BEST":
                continue
            best = pair[-1]
            titles = []
            for candidate in rows:
                first = candidate.words[0]
                if (0 <= first.top - best.top <= 3 * best.height
                        and abs(first.left - word.left) <= 2 * best.height
                        and candidate is not row):
                    titles.append(candidate.words)
            if row.words[index + 2:]:
                titles.append(row.words[index + 2:])
            for title in titles:
                name = " ".join(part.text for part in title).upper()
                if (all(part.confidence >= 55 for part in title)
                        and re.fullmatch(r"[A-Z][A-Z' -]{2,39}", name)
                        and not re.search(r"\b(?:KILL|DESTROYED|NEW BEST)\b", name)):
                    awards.add(name)
    return awards


def kill_badge_match(frame, rows, reference=None):
    if any(KILL_BADGE_PATTERN.search(row.text) for row in rows):
        return "boxed KILL text"
    if any(re.search(r"\bKILL\s+ASSIST\b", row.text, re.IGNORECASE) for row in rows):
        return None
    for word in badge_scores(rows):
        if reference is not None and matches_skull_reference(frame, word, reference):
            return f"saved skull beside score {word.text}"
        if skull_next_to_score(frame, word):
            return f"skull beside score {word.text}"
    return None


KILL_ID_SEPARATOR = "\x00"
# How far after the killer's name to look for a weapon icon, in text heights.
WEAPON_SEARCH_HEIGHTS = 8
# Normalised correlation a saved weapon icon needs. BF6 rocket icons score 0.85-0.95 on
# their clearest frame, while rifles, SMGs and grenades stay below 0.5.
WEAPON_MATCH = .8
# Half-second samples after a kill to keep looking for its weapon icon.
WEAPON_SEARCH_SAMPLES = 10
KILL_COUNT_PREFIX = "Kills: "


def feed_victim(row, killer):
    """The victim's name on a kill line, when OCR read it confidently."""
    names = [word for word in row.words
             if word.left >= killer.right + killer.height and NAME_TOKEN.fullmatch(word.text)
             and word.confidence >= FEED_VICTIM_CONFIDENCE and len(word.text) >= 4]
    return names[-1].text.casefold() if names else None


def same_name(a, b):
    return SequenceMatcher(None, a, b).ratio() >= .7


class KillTracker:
    """Turn per-sample kill-feed matches into one event per kill.

    Each kill adds one line with the player's name to the feed, and the line stays up for
    several seconds while OCR reads it a little differently from frame to frame. Count the
    player's lines and report a rise above the most seen in the last few seconds, so a line
    that flickers out of OCR for a moment is not reported again.

    A faint feed may be read only one line at a time, so the count alone can miss kills in
    quick succession. A line whose victim is read clearly for the first time is also a new
    kill, unless a kill reported shortly before had no readable victim: then it is that one.
    """

    def __init__(self, memory=FEED_KILL_MEMORY, victim_memory=FEED_VICTIM_MEMORY):
        self.counts = deque(maxlen=memory)
        self.victim_memory = victim_memory
        self.victims = {}
        # Samples of reported kills whose victim was not read yet.
        self.unnamed = deque()
        self.sample = -1

    def update(self, lines):
        """Take (row text, vertical position[, victim]) tuples; return IDs of the rows that are new kills."""
        self.sample += 1
        recent = max(self.counts, default=0)
        self.counts.append(len(lines))
        self.victims = {name: seen for name, seen in self.victims.items()
                        if self.sample - seen <= self.victim_memory}
        while self.unnamed and self.sample - self.unnamed[0] > FEED_UNNAMED_MEMORY:
            self.unnamed.popleft()
        fresh = []
        unnamed = []
        for line in lines:
            victim = line[2] if len(line) > 2 else None
            if victim is None:
                unnamed.append(line)
                continue
            known = [name for name in self.victims if same_name(name, victim)]
            if not known and not any(same_name(victim, other[2]) for other in fresh):
                if self.unnamed:
                    # The victim of a kill already reported, read clearly at last.
                    self.unnamed.popleft()
                else:
                    fresh.append(line)
            for name in known:
                self.victims[name] = self.sample
            self.victims[victim] = self.sample
        # Lines whose victim is already known cannot be new kills.
        counted = min(len(lines) - recent, len(fresh) + len(unnamed))
        added = max(counted, len(fresh))
        if added <= 0:
            return set()
        # Name new victims first, then the lowest unnamed lines: the feed adds new lines at the bottom.
        newest = (fresh + sorted(unnamed, key=lambda line: line[1])[::-1])[:added]
        self.unnamed.extend(self.sample for line in newest if all(line is not other for other in fresh))
        return {f"{line[0]}{KILL_ID_SEPARATOR}{self.sample}.{index}" for index, line in enumerate(newest)}


def ocr_events(path, region, reason, identify, duration, progress, *,
               page_mode="11", stop=None, mode=OCR_MODES[0], for_ocr=None, rounding="near",
               hardware_decode=False, describe=None):
    filters = f"fps=2:start_time=0:round={rounding}," + ocr_filter(region, mode)
    command = decode(path, "-filter_threads", "1", "-threads:v", "1")
    command += ["-map", "0:V:0", "-an", "-vf", filters, "-threads:v", "1",
                "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    tesseract_env = {**os.environ, "OMP_THREAD_LIMIT": "1"}
    recent = deque(maxlen=3)
    last_frame = None
    with closing(video_blocks(command, OCR_WIDTH * OCR_HEIGHT, hardware_decode, progress)) as frames:
        for index, data in enumerate(frames):
            if stop is not None and stop.is_set():
                return
            if len(data) != OCR_WIDTH * OCR_HEIGHT:
                raise RuntimeError("FFmpeg returned a partial video frame.")
            timestamp = index / 2
            if index % 20 == 0:
                progress(f"Scanning {reason.lower()} at {timestamp:.0f} / {duration:.0f}s")
            if data != last_frame:
                frame = OCR_HEADER + data
                ocr_frame = for_ocr(frame) if for_ocr else frame
                rows = read_ocr_frame(ocr_frame, page_mode, tesseract_env, mode)
                last_frame = data
            matches = identify(rows, frame)
            previous = set().union(*recent)
            if matches - previous:
                yield timestamp, reason
                if describe:
                    for detail in describe(rows, frame, matches - previous, timestamp):
                        yield timestamp, detail
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
                             tuple(sorted(set(last.reasons) | {reason})), last.cues + ((timestamp, reason),))
        else:
            clips.append(Clip(start, end, (reason,), ((timestamp, reason),)))
    return clips


def validate_detection(track, threshold, player, score, before, after, *, badge=False):
    if track != "auto" and track < 0:
        raise ValueError("Choose a positive audio track number, or use 0 to disable.")
    if not math.isfinite(threshold) or not -90 <= threshold <= 0:
        raise ValueError("Mic threshold must be between -90 and 0 dBFS.")
    if not all(math.isfinite(value) and value >= 0 for value in (before, after)) or after == 0:
        raise ValueError("Lead-in must be nonnegative and follow-through must be positive.")
    player = player.strip()
    score_pattern = re.compile(score, re.IGNORECASE) if score else None
    if not (track or player or score_pattern or badge):
        raise ValueError("Enable the mic, kill feed, KILL badge, or score detector.")
    if (player or score_pattern or badge) and not shutil.which("tesseract"):
        raise RuntimeError("Install Tesseract with English language data for on-screen detection.")
    return player, score_pattern


def scan(path, recording, track, threshold, player, score, before, after, progress=print,
         *, feed=None, diagnostics=None, stop=None, acceleration=VideoAcceleration()):
    badge = feed is not None and feed.badge
    player, score_pattern = validate_detection(track, threshold, player, score, before, after, badge=badge)
    levels = None
    auto = track == "auto"
    if auto:
        try:
            track, levels = detect_mic_track(path, recording, progress)
        except ValueError as error:
            if not (player or score_pattern or badge):
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
    if player and not (feed and feed.badge_feed_details):
        if diagnostics is not None:
            diagnostics.feed_scanned = True
        killer_only = feed is not None and not feed.any_mention
        green_only = feed is not None and feed.mode == GREEN_PLAYER
        player_pattern = player_name_pattern(player)
        # OCR often reads O as 0 or l as 1; the side check below guards against deaths.
        feed_pattern = player_name_pattern(player, allow_lookalikes=True) if feed else player_pattern
        divider = (feed_divider_x(recording.frame_size, feed.region, feed.divider)
                   if feed and feed.divider is not None else None)
        kills = KillTracker()
        # [kill sample, label or None] for kills whose weapon icon is still being looked for.
        weapon_searches = []
        weapon_events = []

        def find_weapons(frame, killers):
            for search in weapon_searches:
                if search[1] is None:
                    for row, word in killers:
                        search[1] = recognize_weapon(frame, row, feed.weapon_references, feed.mode, killer=word)
                        if search[1]:
                            weapon_events.append((search[0] / 2, f"Weapon: {search[1]}"))
                            break
            weapon_searches[:] = [search for search in weapon_searches
                                  if search[1] is None and kills.sample - search[0] < WEAPON_SEARCH_SAMPLES]

        def feed_matches(rows, frame):
            if feed:
                rows = position_feed_rows(rows, divider)
            lines = {row.text.casefold() for row in rows}
            mentions = {line for line in lines if player_pattern.search(line)}
            sides = set()
            green_name = False
            matches = set()
            lookalikes = set()
            lookalike_rows = set()
            killer_rows = []
            killers = []
            counted = []
            if feed:
                for row in rows:
                    for spelling, side, green, word in row.name_words(feed_pattern):
                        if not player_pattern.fullmatch(spelling):
                            lookalikes.add(spelling.casefold())
                            lookalike_rows.add(row.text.casefold())
                        sides.add(side)
                        green_name |= green
                        if (side == "left" or not killer_only) and (green or not green_only):
                            matches.add(row.text.casefold())
                            # OCR sometimes repeats a name within one line; count each line once.
                            if not any(seen is row for seen in counted):
                                counted.append(row)
                                killer_rows.append((row.text.casefold(), word.middle, feed_victim(row, word)))
                            if side == "left":
                                killers.append((row, word))
            else:
                matches = mentions
            if diagnostics is not None:
                diagnostics.feed_mentions += bool(mentions)
                diagnostics.feed_lookalikes.update(lookalikes)
                diagnostics.feed_left_mentions += "left" in sides
                diagnostics.feed_right_mentions += "right" in sides
                diagnostics.feed_unknown_mentions += "unknown" in sides
                if green_only:
                    diagnostics.feed_green_mentions = (diagnostics.feed_green_mentions or 0) + green_name
                diagnostics.feed_matches += bool(matches)
                relevant = mentions | lookalike_rows
                readable = {row.text.casefold() for row in rows if row.readable} | relevant
                diagnostics.feed_uncertain_rows += len(lines - readable)
                for line in sorted(readable, key=lambda line: line not in relevant):
                    line = line[:160]
                    if len(diagnostics.feed_rows) < 100 or line in diagnostics.feed_rows:
                        diagnostics.feed_rows[line] += 1
            if not feed:
                return matches
            new = kills.update(killer_rows)
            if feed.weapon_references:
                weapon_searches.extend([kills.sample, None] for _ in new)
                if weapon_searches:
                    # The icon is often clearest a second or two after the line appears.
                    find_weapons(frame, killers)
            return new

        def describe_feed(_rows, _frame, new_matches, _timestamp):
            if len(new_matches) > 1:
                yield f"{KILL_COUNT_PREFIX}{len(new_matches)}"

        events.extend(ocr_events(path, feed.region if feed else FEED, "Kill feed", feed_matches,
                                 recording.duration, progress, page_mode="6" if feed else "11",
                                 stop=stop, mode=feed.mode if feed else OCR_MODES[0],
                                 hardware_decode=acceleration.decode,
                                 describe=describe_feed if feed else None))
        events.extend(weapon_events)
    if badge:
        if diagnostics is not None:
            diagnostics.badge_samples = 0
            diagnostics.badge_reference_loaded = feed.skull_reference is not None
            if feed.badge_feed_details:
                diagnostics.badge_feed_matches = 0

        def badge_matches(rows, frame):
            evidence = kill_badge_match(frame, rows, feed.skull_reference)
            if diagnostics is not None:
                diagnostics.badge_score_samples += bool(badge_scores(rows))
                diagnostics.badge_samples += bool(evidence)
                diagnostics.badge_reference_matches += bool(evidence and evidence.startswith("saved skull"))
            return {"KILL"} if evidence else set()

        def describe_target(rows, _frame, _matches, timestamp):
            awards = kill_awards(rows)
            if not awards:
                for seconds in (timestamp, timestamp + .5):
                    if seconds >= recording.duration or (stop is not None and stop.is_set()):
                        break
                    try:
                        _, award_rows = preview_ocr_frame(path, seconds, KILL_AWARD_REGION, KILL_BADGE_MODE, "11")
                        awards = kill_awards(award_rows)
                    except (OSError, RuntimeError, ValueError) as error:
                        progress(f"Could not read awards at {seconds:.2f}s. {error}")
                        break
                    if awards:
                        break
            for award in sorted(awards):
                yield f"Award: {award}"
            positioned = position_feed_rows(rows, None)
            if "BALLISTIC" in awards:
                yield "Target: Helicopter (BALLISTIC)"
            elif any(re.search(r"\bHELICOPTER\s+(?:DESTROYED|KILL)\b", row.text, re.IGNORECASE)
                   for row in positioned):
                yield "Target: Helicopter"
            elif any(re.search(r"\bVEHICLE\s+DESTROYED\b", row.text, re.IGNORECASE) for row in positioned):
                yield "Target: Vehicle"
            if feed.badge_feed_details:
                progress(f"Checking kill feed near {timestamp:.2f}s…")
                try:
                    details = badge_feed_details(path, recording, timestamp, rows, player, feed, stop)
                except (OSError, RuntimeError, ValueError) as error:
                    progress(f"Could not read feed details at {timestamp:.2f}s. {error}")
                    return
                if diagnostics is not None:
                    diagnostics.badge_feed_matches += bool(details)
                yield from details

        # Use the last source frame at or before each half-second sample.
        events.extend(ocr_events(path, KILL_BADGE_REGION, "KILL badge", badge_matches,
                                 recording.duration, progress, page_mode=KILL_BADGE_PAGE_MODE,
                                 stop=stop, mode=KILL_BADGE_MODE, for_ocr=badge_text_frame, rounding="up",
                                 hardware_decode=acceleration.decode, describe=describe_target))
    if score_pattern:
        def score_matches(rows, _frame):
            text = "\n".join(row.text for row in rows)
            return {"".join(match.group().upper().split()) for match in score_pattern.finditer(text)}

        events.extend(ocr_events(path, SCORE, "Score popup", score_matches,
                                 recording.duration, progress, hardware_decode=acceleration.decode))
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


def export_clip(source, clip, directory, progress=print, *, audio_offset_ms=0,
                acceleration=VideoAcceleration()):
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
        duration = clip.end - clip.start
        streams = ["-i", str(source), "-ss", f"{clip.start - seek:.3f}",
                   "-t", f"{duration:.3f}",
                   "-map", "0:V:0", "-map", "0:a?", "-map_chapters", "-1",
                   # Preserve the recording's frame cadence and audio timestamp gaps.
                   "-fps_mode", "passthrough", "-af", audio_filter,
                   "-c:a", "flac"]
        encode_video(command, streams, encoded, progress, acceleration, duration)
        return publish_export(encoded, directory, stem)


def encode_video(command, streams, encoded, progress, acceleration, duration):
    radeon = command[:1] + RADEON_DECODE_ARGS + command[1:]
    if acceleration.encode:
        amf = ["-c:v", "h264_amf", "-pix_fmt", "nv12", "-usage", "transcoding", "-quality", "quality",
               "-rc", "cqp", "-qp_i", "18", "-qp_p", "20", "-qp_b", "22"]
        plans = [(radeon, "Radeon decoding and AMF encoding")] if acceleration.decode else []
        plans.append((command, "CPU decoding and Radeon AMF encoding"))
        for options, description in plans:
            progress(f"Trying {description}…")
            try:
                run_export(options + ["-threads:v", "2"] + streams + amf + [str(encoded)],
                           description, progress, duration)
                return
            except RuntimeError as error:
                encoded.unlink(missing_ok=True)
                detail = str(error).partition("\n")[0]
                progress(f"{description} failed. {detail}")
        progress("Using CPU encoding for this clip.")
    if acceleration.decode:
        progress("Trying Radeon decoding with CPU encoding…")
        try:
            encode_x264(radeon, streams, encoded, progress, duration)
            return
        except RuntimeError:
            encoded.unlink(missing_ok=True)
            progress("Radeon decoding failed for this export. Retrying with CPU decoding.")
    encode_x264(command, streams, encoded, progress, duration)


def encode_x264(command, streams, encoded, progress, duration):
    streams = streams + ["-c:v", "libx264", "-crf", "18"]
    description = "Radeon decoding and CPU encoding (x264)" if "-hwaccel" in command else "CPU encoding (x264)"
    try:
        run_export(command + ["-threads:v", "2"] + streams
                   + ["-threads:v", "2", "-preset", "fast", "-x264-params",
                      "rc-lookahead=12:lookahead-threads=1", str(encoded)],
                   description, progress, duration)
    except RuntimeError as error:
        if not re.search(r"malloc of size .* failed|cannot allocate memory|out of memory",
                         str(error), re.IGNORECASE):
            raise
        encoded.unlink(missing_ok=True)
        progress("Encoder ran out of memory. Retrying with a lower-memory preset.")
        run_export(command + ["-threads:v", "1"] + streams
                   + ["-threads:v", "1", "-preset", "ultrafast", str(encoded)],
                   description, progress, duration)


def publish_export(encoded, directory, stem):
    for suffix in range(10000):
        destination = directory / (stem + (f"_{suffix}" if suffix else "") + encoded.suffix)
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


def recording_date(path):
    for match in re.finditer(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", path.stem):
        try:
            return date.fromisoformat(match.group())
        except ValueError:
            continue
    return datetime.fromtimestamp(path.stat().st_mtime).date()


def filter_recording_dates(dates, first, last):
    try:
        start = date.fromisoformat(first) if first else None
        end = date.fromisoformat(last) if last else None
    except ValueError as error:
        raise ValueError("Enter recording dates as YYYY-MM-DD.") from error
    if start and end and start > end:
        raise ValueError("The first recording date must be on or before the last date.")
    matches = [path for path, day in dates.items()
               if (start is None or day >= start) and (end is None or day <= end)]
    if not matches:
        raise ValueError("No recordings in that date range.")
    return matches


def clip_preview_frames(source, clip):
    frames = []
    length = clip.end - clip.start
    for position in (.08, .5, .92):
        seconds = clip.start + length * position
        image = run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{seconds:.3f}",
                     "-filter_threads", "1", "-threads:v", "1", "-i", str(source),
                     "-map", "0:V:0", "-frames:v", "1",
                     "-vf", "scale=160:90:force_original_aspect_ratio=decrease,pad=160:90:(ow-iw)/2:(oh-ih)/2",
                     "-c:v", "ppm", "-f", "image2pipe", "pipe:1"])
        frames.append((seconds, image))
    return frames


def prepare_output(sources, output):
    destination = output.resolve()
    if any(source.parent.resolve() == destination for source in sources):
        raise ValueError("Choose a separate output folder so exports are excluded from future scans.")
    output.mkdir(parents=True, exist_ok=True)


def validate_batch(options, *, badge=False):
    validate_detection(*options, badge=badge)
    for program in ("ffmpeg", "ffprobe"):
        if not shutil.which(program):
            raise RuntimeError(f"Install {program} and add it to PATH.")


def scan_recording(source, options, progress, *, feed=None, stop=None, acceleration=VideoAcceleration()):
    result = BatchResult(source, [], [], [])
    if stop is not None and stop.is_set():
        return result
    progress("Reading recording")
    try:
        recording = probe(source)
        result.audio_tracks = len(recording.audio)
        result.clips = scan(source, recording, *options, progress=progress, feed=feed,
                            diagnostics=result if feed else None, stop=stop, acceleration=acceleration)
        if feed and feed.fallback_track and not result.clips:
            result.clips = loud_fallback(source, recording, feed, *options[-2:], progress, stop)
    except (OSError, RuntimeError, ValueError) as error:
        result.errors.append(str(error))
    return result


def process_batch(sources, options, output=None, progress=print, *, audio_offset_ms=0,
                  acceleration=VideoAcceleration()):
    validate_batch(options)
    if output is not None:
        prepare_output(sources, output)
    total = len(sources)
    for index, source in enumerate(sources, 1):
        prefix = f"[{index}/{total}] {source.name}"

        def report(message):
            progress(f"{prefix} · {message}")

        result = scan_recording(source, options, report, acceleration=acceleration)
        if output is not None:
            export_candidates(result, output, report, audio_offset_ms, acceleration)
        yield result


def export_candidates(result, output, progress, audio_offset_ms, acceleration):
    for count, clip in enumerate(result.clips, 1):
        progress(f"Exporting clip {count}/{len(result.clips)} ({clip.start:.2f}–{clip.end:.2f}s)")

        def report(message):
            progress(f"Clip {count}/{len(result.clips)} · {message}")

        try:
            result.exports.append(export_clip(result.source, clip, output, progress=report,
                                              audio_offset_ms=audio_offset_ms, acceleration=acceleration))
        except (OSError, RuntimeError) as error:
            result.errors.append(f"Clip at {clip.start:.2f}s: {error}")


def montage_segment_info(path):
    info = json.loads(run(["ffprobe", "-v", "error", "-show_entries",
                           "stream=codec_type,codec_name,time_base,width,height,pix_fmt,sample_aspect_ratio,"
                           "channels,channel_layout,sample_rate,sample_fmt:stream_tags=title,language:format=duration",
                           "-of", "json", str(path)]))
    duration = float(info.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Montage segment needs a readable duration.")
    return info["streams"], duration


def compatible_montage_layout(layout, expected):
    if layout == expected[:len(layout)]:
        return True
    if len(layout) != 2 or len(expected) <= 2 or layout[0] != expected[0]:
        return False
    # A single legacy audio track occupies the main audio slot even if its title differs.
    return ({key: value for key, value in layout[1].items() if key != "tags"}
            == {key: value for key, value in expected[1].items() if key != "tags"})


def pad_montage_audio(path, destination, present, expected):
    missing = expected[len(present):]
    if not missing:
        return
    duration = probe(path).duration
    padded = path.with_name("working-" + destination.name)
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path)]
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
    padded.replace(destination)


@dataclass
class MontageSegment:
    path: Path
    layout: list[dict]
    duration: float
    result: BatchResult
    clip: Clip = None


def audio_slot_order(streams, preferred):
    """Align unique shared titles first, then use the original track positions."""
    titles = [stream.get("tags", {}).get("title", "").casefold().strip() for stream in streams]
    slots = [None] * len(preferred)
    remaining = []
    for index, title in enumerate(titles):
        if title and titles.count(title) == preferred.count(title) == 1:
            slots[preferred.index(title)] = index
        else:
            remaining.append(index)
    for index in remaining:
        slots[index if slots[index] is None else slots.index(None)] = index
    return slots


def montage_audio_settings(segments, reference):
    preferred = [stream.get("tags", {}).get("title", "").casefold().strip() for stream in reference]
    orders = [audio_slot_order(segment.layout[1:], preferred) for segment in segments]
    settings = []
    for index, reference_stream in enumerate(reference):
        present = [segment.layout[order[index] + 1] for segment, order in zip(segments, orders)
                   if order[index] is not None]
        richest = max(present, key=lambda stream: stream["channels"])
        tags = dict(reference_stream.get("tags", {}))
        titles = {stream.get("tags", {}).get("title", "").casefold().strip() for stream in present}
        if len(titles - {""}) > 1:
            tags["title"] = f"Audio track {index + 1}"
        settings.append({
            "channel_layout": richest.get("channel_layout") or f'{richest["channels"]}c',
            "tags": tags,
        })
    return settings, orders


def normalize_montage_segment(path, destination, width, height, audio, order, progress, acceleration):
    converted = path.with_name("working-" + destination.name)
    converted.unlink(missing_ok=True)
    duration = probe(path).duration
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-filter_threads", "1"]
    streams = ["-i", str(path)]
    silence = {}
    for slot, stream in enumerate(audio):
        if order[slot] is None:
            silence[slot] = len(silence) + 1
            streams += ["-f", "lavfi", "-i", f'anullsrc=r=48000:cl={stream["channel_layout"]}']
    streams += ["-t", f"{duration:.3f}", "-map", "0:V:0", "-map_chapters", "-1"]
    for slot, stream in enumerate(audio):
        index = order[slot]
        streams += ["-map", f"0:a:{index}" if index is not None else f"{silence[slot]}:a:0",
                    f"-channel_layout:a:{slot}", stream["channel_layout"],
                    f"-filter:a:{slot}", "aresample=48000:async=1:first_pts=0,apad"]
    # Fit the display aspect ratio inside the first recording's canvas.
    filters = (f"scale=w='max(2,trunc(min({width},{height}*dar)/2)*2)':"
               f"h='max(2,trunc(min({height},{width}/dar)/2)*2)',"
               f"setsar=1,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p")
    streams += ["-vf", filters, "-fps_mode", "passthrough", "-c:a", "flac", "-sample_fmt:a", "s32"]
    encode_video(command, streams, converted, progress, acceleration, duration)
    converted.replace(destination)


def scan_kills(sources, player, before, after, progress=print, *,
               workers=DEFAULT_MONTAGE_WORKERS, stop=None, feed=KillFeedOptions(),
               acceleration=VideoAcceleration()):
    if not player.strip() and not feed.badge:
        raise ValueError("Enter your player name to make a kill montage.")
    if workers < 1:
        raise ValueError("Choose at least one montage scan worker.")
    options = (0, feed.threshold, player, "", before, after)
    validate_batch(options, badge=feed.badge)
    if not sources:
        return
    if stop is None:
        stop = Event()

    def scan_file(index, source):
        def report(message):
            progress(f"[{index}/{len(sources)}] {source.name} · {message}")

        return scan_recording(source, options, report, feed=feed, stop=stop, acceleration=acceleration)

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


def save_montage_job(sources, candidates, output, audio_offset_ms, *, output_format="mkv", mic_mix_track=None,
                     style=None):
    prepare_output(sources, output)
    source_order = {source: index for index, source in enumerate(sources)}
    recordings = []
    for result in sorted(candidates, key=lambda item: source_order[item.source]):
        source = result.source.resolve()
        stat = source.stat()
        recordings.append({"path": str(source), "size": stat.st_size, "modified": stat.st_mtime_ns,
                           "clips": [[clip.start, clip.end, clip.reasons, clip.cues] for clip in result.clips]})
    settings = {"version": 1, "audio_offset_ms": audio_offset_ms, "recordings": recordings}
    if output_format != "mkv":
        settings["output_format"] = output_format
    if mic_mix_track is not None:
        settings["mic_mix_track"] = mic_mix_track
    if style is not None and style.active:
        settings["style"] = asdict(style)
    plan = json.dumps(settings, ensure_ascii=False).encode("utf-8")
    directory = output / MONTAGE_CHECKPOINTS / hashlib.sha256(plan).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    job = directory / "job.json"
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(plan)
    temporary.replace(job)
    return job


def latest_montage_job(output):
    if output is None:
        return None
    try:
        return max((output / MONTAGE_CHECKPOINTS).glob("*/job.json"),
                   key=lambda path: path.stat().st_mtime_ns, default=None)
    except OSError:
        return None


@contextmanager
def lock_montage_job(directory):
    with (directory / "job.lock").open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            def lock():
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

            def unlock():
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def lock():
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def unlock():
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        try:
            lock()
        except OSError as error:
            raise RuntimeError("This montage is already running in another QuickClip window.") from error
        try:
            yield
        finally:
            unlock()


def resume_montage_job(job, output, progress=print, *, acceleration=VideoAcceleration(), stop=None):
    with lock_montage_job(job.parent):
        plan = json.loads(job.read_text(encoding="utf-8"))
        if plan.get("version") != 1:
            raise ValueError("This saved montage uses an unsupported format.")
        candidates = []
        for recording in plan["recordings"]:
            source = Path(recording["path"])
            stat = source.stat()
            if (stat.st_size, stat.st_mtime_ns) != (recording["size"], recording["modified"]):
                raise ValueError(f"{source.name} changed since this montage started. Build a new montage for that recording.")
            clips = [Clip(item[0], item[1], tuple(item[2]),
                          tuple((float(time), str(reason)) for time, reason in (item[3] if len(item) > 3 else ())))
                     for item in recording["clips"]]
            candidates.append(BatchResult(source, clips, [], []))
        results, path = write_montage([result.source for result in candidates], candidates, output, progress,
                                     audio_offset_ms=plan["audio_offset_ms"], acceleration=acceleration,
                                     checkpoint=job.parent, stop=stop, output_format=plan.get("output_format", "mkv"),
                                     mic_mix_track=plan.get("mic_mix_track"),
                                     style=MontageStyle(**plan["style"]) if plan.get("style") else None)
        if path and not any(result.errors for result in results):
            job.unlink()
            for cached in [*job.parent.glob("*.mkv"), job.parent / "montage.mp4", job.parent / "segments.txt"]:
                try:
                    cached.unlink(missing_ok=True)
                except OSError as error:
                    progress(f"Could not remove montage cache file {cached.name}. {error}")
        return results, path


def check_montage_pause(stop):
    if stop is not None and stop.is_set():
        raise MontagePaused


def write_montage(sources, candidates, output, progress=print, *, audio_offset_ms=0,
                  acceleration=VideoAcceleration(), checkpoint=None, stop=None,
                  output_format="mkv", mic_mix_track=None, style=None):
    style = style or MontageStyle()
    if output_format not in ("mkv", "mp4"):
        raise ValueError("Choose MKV or MP4 for the montage.")
    if mic_mix_track is not None and mic_mix_track < 1:
        raise ValueError("Enter a positive mic track number for the montage mix, or leave it blank.")
    prepare_output(sources, output)
    source_order = {source: index for index, source in enumerate(sources)}
    workspace = nullcontext(checkpoint) if checkpoint else tempfile.TemporaryDirectory(
        prefix=".quickclip-montage-", dir=output)
    with workspace as temp:
        staging = Path(temp)
        results = []
        segments = []
        for result in candidates:
            results.append(result)
            source_index = source_order[result.source]
            for clip_index, clip in enumerate(result.clips, 1):
                check_montage_pause(stop)
                path = staging / f"segment-{source_index:06d}-{clip_index:06d}.mkv"
                label = f"{result.source.name} · Clip {clip_index}/{len(result.clips)}"

                def report(message):
                    progress(f"{label} · {message}")

                try:
                    layout = None
                    if path.is_file():
                        try:
                            layout, duration = montage_segment_info(path)
                            report("Reusing encoded clip.")
                        except (OSError, RuntimeError, ValueError):
                            path.unlink(missing_ok=True)
                    if layout is None:
                        report(f"Exporting {clip.start:.2f}–{clip.end:.2f}s")
                        with tempfile.TemporaryDirectory(prefix=".clip-", dir=staging) as scratch:
                            encoded = export_clip(result.source, clip, Path(scratch), progress=report,
                                                  audio_offset_ms=audio_offset_ms, acceleration=acceleration)
                            layout, duration = montage_segment_info(encoded)
                            encoded.replace(path)
                except (OSError, RuntimeError, ValueError) as error:
                    result.errors.append(f"Clip at {clip.start:.2f}s: {error}")
                    continue
                result.exports.append(path)
                segments.append(MontageSegment(path, layout, duration, result, clip))
        check_montage_pause(stop)
        if not segments:
            return results, None
        segments.sort(key=lambda segment: source_order[segment.result.source])
        if style.active:
            return results, styled_montage(segments, staging, output, output_format, mic_mix_track, style,
                                           progress, acceleration, stop)
        expected = max((segment.layout for segment in segments), key=len)
        mixed = any(not compatible_montage_layout(segment.layout, expected) for segment in segments)
        audio = expected[1:]
        orders = [()] * len(segments)
        if mixed:
            first_video = segments[0].layout[0]
            width = max(2, first_video["width"] // 2 * 2)
            height = max(2, first_video["height"] // 2 * 2)
            audio, orders = montage_audio_settings(segments, audio)
            progress(f"Converting mixed recordings to a {width}×{height} montage…")
        if mic_mix_track is not None and (len(audio) < 2 or mic_mix_track > len(audio)):
            progress(f"Montage has {len(audio)} audio track(s). Mic mix for track {mic_mix_track} skipped.")
            mic_mix_track = None
        ready = []
        for index, (segment, order) in enumerate(zip(segments, orders), 1):
            check_montage_pause(stop)
            try:
                if mixed:
                    key = hashlib.sha256(json.dumps([width, height, audio, order], sort_keys=True).encode()).hexdigest()[:12]
                    destination = staging / f"ready-{key}-{segment.path.name}"

                    def report(message):
                        progress(f"Converting clip {index}/{len(segments)} · {message}")

                    if destination.is_file():
                        report("Reusing converted clip.")
                    else:
                        report(segment.result.source.name)
                        normalize_montage_segment(segment.path, destination, width, height, audio,
                                                  order, report, acceleration)
                elif len(segment.layout) < len(expected):
                    key = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()[:12]
                    destination = staging / f"padded-{key}-{segment.path.name}"
                    if not destination.is_file():
                        pad_montage_audio(segment.path, destination, segment.layout, expected)
                else:
                    destination = segment.path
                duration = segment.duration if destination == segment.path else montage_segment_info(destination)[1]
            except (OSError, RuntimeError, ValueError) as error:
                segment.result.errors.append(f"Could not prepare {segment.path.name} for the montage. {error}")
                segment.result.exports.remove(segment.path)
                continue
            ready.append((destination.name, duration))
        check_montage_pause(stop)
        if not ready:
            return results, None
        total_duration = sum(duration for _, duration in ready)
        progress(f"Joining montage · 0% · {len(ready)} highlight segment(s)")
        manifest = staging / "segments.txt"
        manifest.write_text("".join(f"file '{name}'\n" for name, _ in ready), encoding="ascii")
        encoded = staging / f"montage.{output_format}"
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "concat", "-i", str(manifest), "-map", "0:v:0", "-c:v", "copy"]
        title_keys = ("title", "handler_name") if output_format == "mp4" else ("title",)
        if mic_mix_track is not None:
            mic = mic_mix_track - 1
            other_tracks = [index for index in range(len(audio)) if index != mic]
            main = next((index for index in other_tracks if audio[index].get("tags", {}).get("title", "")
                         .casefold() in ("game", "game audio")), other_tracks[0])
            stereo = "aformat=sample_rates=48000:channel_layouts=stereo"
            command += ["-filter_complex",
                        f"[0:a:{main}]{stereo}[main];[0:a:{mic}]{stereo}[mic];"
                        "[main][mic]amix=inputs=2:duration=longest[mix]",
                        "-map", "[mix]", "-disposition:a:0", "default"]
            for key in title_keys:
                command += ["-metadata:s:a:0", f"{key}=Game + mic"]
        command += ["-map", "0:a?"]
        if output_format == "mp4":
            command += ["-c:a", "aac", "-b:a", "320k"]
        else:
            command += ["-c:a", "copy"]
            if mic_mix_track is not None:
                command += ["-c:a:0", "flac"]
        for index, stream in enumerate(audio, start=int(mic_mix_track is not None)):
            if mic_mix_track is not None:
                command += [f"-disposition:a:{index}", "0"]
            tags = stream.get("tags", {})
            for key in (*title_keys, "language"):
                command += [f"-metadata:s:a:{index}", f"{key}={tags.get('title' if key == 'handler_name' else key, '')}"]
        run_export(command + [str(encoded)], "Joining montage", progress, total_duration)
        destination = publish_export(encoded, output, "kill-montage")
        progress(f"Joining montage · 100% · {output_format.upper()} saved")
        return results, destination


TRANSITIONS = ("None", "Flip", "Whip pan", "Zoom punch", "Flash", "Pixelate", "Slide", "Random")
VERTICAL_MODES = ("Off", "9:16 blurred fill", "9:16 centre crop")
VERTICAL_SIZE = (1080, 1920)
# Kills this close together count as one multi-kill for slow motion.
MULTI_KILL_GAP = 3.0
KILL_CUES = ("Kill feed", "KILL badge")
MIC_BEFORE_KILL, MIC_AFTER_KILL = 1.0, 2.5


@dataclass(frozen=True)
class MontageStyle:
    transition: str = "None"
    transition_seconds: float = .4
    vertical: str = "Off"
    slowmo_multikills: bool = False
    slowmo_speed: float = .5
    mic_at_kills: bool = False

    def __post_init__(self):
        if self.transition not in TRANSITIONS:
            raise ValueError(f"Choose a transition: {', '.join(TRANSITIONS)}.")
        if self.vertical not in VERTICAL_MODES:
            raise ValueError(f"Choose a vertical mode: {', '.join(VERTICAL_MODES)}.")
        if not .1 <= self.transition_seconds <= 2:
            raise ValueError("Transition length must be between 0.1 and 2 seconds.")
        if not .25 <= self.slowmo_speed < 1:
            raise ValueError("Slow motion speed must be between 0.25 and 1.")

    @property
    def active(self):
        return (self.transition != "None" or self.vertical != "Off"
                or self.slowmo_multikills or self.mic_at_kills)


def clip_kills(clip):
    """Kill times relative to the clip start, each with how many kills the feed showed at once."""
    counts = {}
    for time, reason in clip.cues:
        if reason in KILL_CUES:
            counts.setdefault(time, 1)
        elif reason.startswith(KILL_COUNT_PREFIX):
            counts[time] = max(counts.get(time, 1), int(reason.removeprefix(KILL_COUNT_PREFIX)))
    return sorted((time - clip.start, count) for time, count in counts.items())


def multi_kill_windows(kills, duration):
    windows = []
    group = []
    for time, count in kills + [(math.inf, 0)]:
        if group and time - group[-1][0] > MULTI_KILL_GAP:
            if sum(count for _, count in group) >= 2:
                windows.append((max(0, group[0][0] - .4), min(duration, group[-1][0] + 1.2)))
            group = []
        group.append((time, count))
    return [window for window in windows if window[1] - window[0] > .2]


def speed_parts(duration, windows, speed):
    """Split 0..duration into (start, end, speed) parts."""
    parts = []
    position = 0
    for start, end in windows:
        if start > position:
            parts.append((position, start, 1))
        parts.append((start, end, speed))
        position = end
    if position < duration:
        parts.append((position, duration, 1))
    return parts


def mic_envelope(kills, ramp=.15):
    terms = [f"max(0,min(1,min((t-{time - MIC_BEFORE_KILL:.3f})/{ramp},({time + MIC_AFTER_KILL:.3f}-t)/{ramp})))"
             for time, _ in kills]
    if not terms:
        return "0"
    expression = terms[0]
    for term in terms[1:]:
        expression = f"max({expression},{term})"
    return expression


def flip_expression():
    """A card flip: the first clip folds to a vertical line, then the next unfolds from it."""
    def sample(clip, scale):
        x = f"(W/2+(X-W/2)/max({scale},0.001))"
        pixel = f"if(eq(PLANE,0),{clip}0({x},Y),if(eq(PLANE,1),{clip}1({x},Y),{clip}2({x},Y)))"
        return f"if(lt(abs(X-W/2),{scale}*W/2),{pixel},if(eq(PLANE,0),16,128))"
    return f"if(gt(P,0.5),{sample('a', '(2*P-1)')},{sample('b', '(1-2*P)')})"


def transition_filter(name, seconds):
    """Return the xfade options and any filter to apply to the finished transition."""
    xfade = {"Flip": "custom:expr='" + flip_expression() + "'", "Whip pan": "slideleft",
             "Zoom punch": "zoomin", "Flash": "fadewhite", "Pixelate": "pixelize", "Slide": "smoothup"}[name]
    after = ",tmix=frames=5" if name == "Whip pan" else ""
    return f"xfade=transition={xfade}:duration={seconds:.3f}:offset=0{after}"


@dataclass
class StyledSegment:
    path: Path
    duration: float
    parts: list
    main: int | None
    mic: int | None
    kills: list
    rate: str
    hdr: bool
    source_duration: float


def styled_streams(segment, index, style, size, can_tonemap):
    """Filters that turn input `index` into a styled [v{index}] and stereo [a{index}]."""
    width, height = size
    video = f"[{index}:v:0]setpts=PTS-STARTPTS,fps={segment.rate}"
    if segment.hdr and can_tonemap:
        # Bring HLG/PQ captures to SDR so phones and social sites show the right colours.
        tonemap = ("zscale=t=linear:npl=203,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
                   "zscale=t=bt709:m=bt709:r=tv")
    else:
        tonemap = None
    if style.vertical == "9:16 blurred fill":
        geometry = (f"split=2[bg{index}][fg{index}];"
                    f"[bg{index}]scale={width // 4}:{height // 4}:force_original_aspect_ratio=increase,"
                    f"crop={width // 4}:{height // 4},boxblur=12:2,scale={width}:{height}[bgs{index}];"
                    f"[fg{index}]scale={width}:-2[fgs{index}];[bgs{index}][fgs{index}]overlay=(W-w)/2:(H-h)/2")
    elif style.vertical == "9:16 centre crop":
        geometry = f"scale=-2:{height},crop={width}:{height}"
    else:
        geometry = (f"scale=w='max(2,trunc(min({width},{height}*dar)/2)*2)':"
                    f"h='max(2,trunc(min({height},{width}/dar)/2)*2)',pad={width}:{height}:(ow-iw)/2:(oh-ih)/2")
    # Frame first, then tone map and convert the (smaller) final picture.
    finish = (("," + tonemap) if tonemap else "") + ",format=yuv420p"
    graph = []
    head = video
    audio = (f"[{index}:a:{segment.main}]" if segment.main is not None
             else f"anullsrc=r=48000:cl=stereo,atrim=duration={segment.source_duration:.3f}[silence{index}];[silence{index}]")
    audio += "aresample=48000:async=1:first_pts=0,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
    if segment.mic is not None:
        envelope = mic_envelope(segment.kills) if style.mic_at_kills else "1"
        graph.append(f"[{index}:a:{segment.mic}]aresample=48000:async=1:first_pts=0,"
                     f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                     f"volume='{envelope}':eval=frame[mic{index}]")
        audio += f"[game{index}];[game{index}][mic{index}]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95"
    if len(segment.parts) > 1:
        graph.append(head + f",split={len(segment.parts)}" + "".join(f"[vp{index}_{n}]" for n in range(len(segment.parts))))
        graph.append(audio + f",asplit={len(segment.parts)}" + "".join(f"[ap{index}_{n}]" for n in range(len(segment.parts))))
        pieces = []
        for n, (start, end, speed) in enumerate(segment.parts):
            graph.append(f"[vp{index}_{n}]trim=start={start:.3f}:end={end:.3f},setpts=(PTS-STARTPTS)/{speed}[vq{index}_{n}]")
            tempo = "" if speed == 1 else (f",atempo={speed}" if speed >= .5 else f",atempo=0.5,atempo={speed / .5:.4f}")
            graph.append(f"[ap{index}_{n}]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS"
                         + tempo + f"[aq{index}_{n}]")
            pieces.append(f"[vq{index}_{n}][aq{index}_{n}]")
        graph.append("".join(pieces) + f"concat=n={len(segment.parts)}:v=1:a=1[vs{index}][a{index}]")
        graph.append(f"[vs{index}]fps={segment.rate}," + geometry + finish + f",setsar=1[v{index}]")
    else:
        graph.append(head + "," + geometry + finish + f",setsar=1[v{index}]")
        graph.append(audio + f"[a{index}]")
    return graph


def styled_montage(segments, staging, output, output_format, mic_mix_track, style, progress, acceleration, stop):
    """Build a montage with transitions, slow motion, vertical framing and a mic mix.

    Each clip body and each transition is encoded on its own, then the pieces are joined
    without re-encoding, so the work grows only with the number of clips.
    """
    if style.mic_at_kills and mic_mix_track is None:
        raise ValueError("Choose the mic track to mix in at kill time.")
    can_tonemap = "zscale" in ffmpeg_features("ffmpeg", "-filters")
    styled = []
    for segment in segments:
        streams, duration = montage_segment_info(segment.path)
        video = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "V:0", "-show_entries",
                                "stream=width,height,avg_frame_rate,r_frame_rate,color_transfer",
                                "-of", "json", str(segment.path)]))["streams"][0]
        audio = [stream for stream in streams if stream["codec_type"] == "audio"]
        titles = [stream.get("tags", {}).get("title", "").casefold().strip() for stream in audio]
        mic = mic_mix_track - 1 if mic_mix_track is not None and mic_mix_track <= len(audio) else None
        others = [index for index in range(len(audio)) if index != mic]
        main = next((index for index in others if titles[index] in ("game", "game audio")),
                    others[0] if others else None)
        kills = clip_kills(segment.clip)
        windows = multi_kill_windows(kills, duration) if style.slowmo_multikills else []
        parts = speed_parts(duration, windows, style.slowmo_speed)
        rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "60"
        if rate in ("0/0", "0"):
            rate = "60"
        styled.append(StyledSegment(segment.path, sum((end - start) / speed for start, end, speed in parts), parts,
                                    main, mic, kills, rate,
                                    video.get("color_transfer") in ("arib-std-b67", "smpte2084"), duration))
        if len(styled) == 1:
            first = video
    size = VERTICAL_SIZE if style.vertical != "Off" else (max(2, first["width"] // 2 * 2), max(2, first["height"] // 2 * 2))
    rate = styled[0].rate
    for item in styled:
        item.rate = rate
    gap = style.transition_seconds if style.transition != "None" else 0
    if gap:
        gap = min(gap, min(item.duration for item in styled) / 3)
    # Random goes through every transition in a shuffled order before repeating one.
    names = [name for name in TRANSITIONS if name not in ("None", "Random")]
    Random(":".join(item.path.name for item in styled)).shuffle(names)
    sdr = can_tonemap or not any(item.hdr for item in styled)
    key = hashlib.sha256(json.dumps([asdict(style), mic_mix_track, size, gap, can_tonemap,
                                     [item.path.name for item in styled]]).encode()).hexdigest()[:12]
    pieces = []
    total = sum(item.duration for item in styled) - gap * (len(styled) - 1)

    def encode_piece(name, inputs, graph, label, duration):
        destination = staging / f"styled-{key}-{name}.mkv"
        pieces.append(destination)
        if destination.is_file():
            progress(f"{label} · Reusing encoded piece.")
            return
        check_montage_pause(stop)
        working = staging / f"working-{destination.name}"
        working.unlink(missing_ok=True)
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-filter_threads", "2"]
        streams = []
        for path in inputs:
            streams += ["-i", str(path)]
        streams += ["-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]", "-c:a", "flac"]
        if sdr:
            streams += ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
        encode_video(command, streams, working, lambda message: progress(f"{label} · {message}"),
                     acceleration, duration)
        working.replace(destination)

    for index, item in enumerate(styled):
        head = gap if index > 0 else 0
        tail = gap if index < len(styled) - 1 else 0
        graph = styled_streams(item, 0, style, size, can_tonemap)
        graph.append(f"[v0]trim=start={head:.3f}:end={item.duration - tail:.3f},setpts=PTS-STARTPTS[vout]")
        graph.append(f"[a0]atrim=start={head:.3f}:end={item.duration - tail:.3f},asetpts=PTS-STARTPTS[aout]")
        encode_piece(f"{index:04d}-body", [item.path], graph, f"Styling clip {index + 1}/{len(styled)}",
                     item.duration - head - tail)
        if tail:
            following = styled[index + 1]
            name = style.transition if style.transition != "Random" else names[index % len(names)]
            graph = styled_streams(item, 0, style, size, can_tonemap) + styled_streams(following, 1, style, size, can_tonemap)
            graph.append(f"[v0]trim=start={item.duration - gap:.3f},setpts=PTS-STARTPTS[va]")
            graph.append(f"[v1]trim=end={gap:.3f},setpts=PTS-STARTPTS[vb]")
            graph.append(f"[va][vb]{transition_filter(name, gap)},format=yuv420p[vout]")
            graph.append(f"[a0]atrim=start={item.duration - gap:.3f},asetpts=PTS-STARTPTS[aa]")
            graph.append(f"[a1]atrim=end={gap:.3f},asetpts=PTS-STARTPTS[ab]")
            graph.append(f"[aa][ab]acrossfade=d={gap:.3f}:c1=tri:c2=tri[aout]")
            encode_piece(f"{index:04d}-transition", [item.path, following.path], graph,
                         f"{name} transition {index + 1}/{len(styled) - 1}", gap)
    check_montage_pause(stop)
    progress(f"Joining montage · 0% · {len(styled)} highlight segment(s)")
    manifest = staging / f"styled-{key}.txt"
    manifest.write_text("".join(f"file '{path.name}'\n" for path in pieces), encoding="utf-8")
    encoded = staging / f"montage.{output_format}"
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "concat", "-safe", "0", "-i", str(manifest), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy"]
    command += ["-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart"] if output_format == "mp4" else ["-c:a", "copy"]
    title = "Game + mic" if mic_mix_track is not None else "Game"
    for key_name in (("title", "handler_name") if output_format == "mp4" else ("title",)):
        command += ["-metadata:s:a:0", f"{key_name}={title}"]
    run_export(command + [str(encoded)], "Joining montage", progress, total)
    destination = publish_export(encoded, output, "kill-montage-vertical" if style.vertical != "Off" else "kill-montage")
    progress(f"Joining montage · 100% · {output_format.upper()} saved")
    return destination


def settings_path():
    """Where the desktop app keeps its settings, including saved weapon icons."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "QuickClip" / "settings.json"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "quickclip" / "settings.json"


def load_weapon_references(path):
    try:
        values = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    references = []
    for item in values.get("weapon_references", ()) if isinstance(values, dict) else ():
        try:
            references.append(WeaponReference(item["label"], item["mode"], tuple(item["box"]), tuple(item["pixels"])))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(references)


PRESETS = Path(__file__).resolve().with_name("presets")


def load_presets():
    """Built-in game presets: feed area, OCR treatment, montage cue and weapon icons."""
    presets = {}
    for path in sorted(PRESETS.glob("*.json")):
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
            references = tuple(WeaponReference(item["label"], item["mode"], tuple(item["box"]), tuple(item["pixels"]))
                               for item in values.get("weapon_references", ()))
            parse_feed_region(values["feed_region"])
            if values["feed_mode"] not in OCR_MODES:
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        presets[path.stem] = {**values, "weapon_references": references}
    return presets


def merge_weapon_references(saved, extra):
    """Add extra icons, keeping a saved icon with the same label and treatment instead."""
    have = {(reference.label.casefold(), reference.mode) for reference in saved}
    return tuple(saved) + tuple(reference for reference in extra if (reference.label.casefold(), reference.mode) not in have)


def parse_weapon_filter(text):
    """Comma-separated weapon labels, compared without regard to case."""
    return frozenset(label.strip().casefold() for label in text.split(",") if label.strip())


def clip_weapons(clip):
    return {reason.removeprefix("Weapon: ").casefold() for reason in clip.reasons if reason.startswith("Weapon: ")}


def matches_weapons(clip, wanted):
    return not wanted or bool(clip_weapons(clip) & wanted)


def only_weapons(candidates, wanted):
    """Keep only clips with one of the wanted weapons."""
    for result in candidates:
        if wanted:
            result.clips = [clip for clip in result.clips if matches_weapons(clip, wanted)]
        yield result


def kill_montage(sources, player, before, after, output, progress=print, *,
                 audio_offset_ms=0, workers=DEFAULT_MONTAGE_WORKERS, feed=KillFeedOptions(),
                 acceleration=VideoAcceleration(), output_format="mkv", mic_mix_track=None, style=None,
                 weapons=frozenset()):
    with closing(scan_kills(sources, player, before, after, progress, workers=workers,
                            feed=feed, acceleration=acceleration)) as candidates:
        return write_montage(sources, only_weapons(candidates, weapons), output, progress, audio_offset_ms=audio_offset_ms,
                             acceleration=acceleration, output_format=output_format, mic_mix_track=mic_mix_track,
                             style=style)


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
    parser.add_argument("--from-date", default="", metavar="YYYY-MM-DD", help="First recording date to include.")
    parser.add_argument("--to-date", default="", metavar="YYYY-MM-DD", help="Last recording date to include.")
    parser.add_argument("--kill-montage", action="store_true",
                        help="Join kill matches into one file. Requires --output and either --player or --kill-badge.")
    parser.add_argument("--montage-format", choices=("mkv", "mp4"),
                        help="Container for the joined montage (default: mkv).")
    parser.add_argument("--mix-mic-track", type=int, metavar="N",
                        help="Add 1-based audio track N to the default montage playback mix.")
    parser.add_argument("--transition", choices=[name.lower().replace(" ", "-") for name in TRANSITIONS],
                        default="none", help="Montage transition between clips.")
    parser.add_argument("--vertical", choices=("off", "blur", "crop"), default="off",
                        help="Make a 9:16 montage for TikTok/Shorts: blurred fill or centre crop.")
    parser.add_argument("--slowmo-multikills", action="store_true",
                        help="Play multi-kills at half speed in the montage.")
    parser.add_argument("--mic-at-kills", action="store_true",
                        help="With --mix-mic-track, mix the mic in only around each kill.")
    parser.add_argument("--settings", type=Path, metavar="FILE",
                        help="Desktop settings file to read saved weapon icons from (default: the app's own).")
    parser.add_argument("--only-weapon", action="append", default=[], metavar="LABEL",
                        help="Montage only kills with this saved weapon label, such as Rocket. Repeat for more.")
    parser.add_argument("--kill-badge", action="store_true",
                        help="Detect Battlefield's on-screen KILL label in montage mode. Omit --player for badge only.")
    parser.add_argument("--badge-feed-details", action="store_true",
                        help="Scan for kill badges, then read the kill feed around each hit in montage mode.")
    parser.add_argument("--workers", type=int, default=DEFAULT_MONTAGE_WORKERS,
                        help=f"Recordings to scan at once in montage mode (default {DEFAULT_MONTAGE_WORKERS}).")
    parser.add_argument("--preset", choices=sorted(load_presets()),
                        help="Game preset for the kill-feed area, text treatment and weapon icons, such as battlefield6.")
    parser.add_argument("--feed-region", metavar="L,T,W,H",
                        help="Montage kill-feed crop as percentages of the full frame.")
    parser.add_argument("--feed-divider", metavar="PERCENT",
                        help="Use a fixed split measured from the left edge of the crop. Defaults to per-row detection.")
    parser.add_argument("--feed-text-mode", choices=OCR_MODE_ARGS,
                        help="Image treatment used on the montage kill-feed crop.")
    parser.add_argument("--any-feed-mention", action="store_true",
                        help="Include any OCR line mentioning the player in a montage, including deaths.")
    parser.add_argument("--montage-audio-fallback", action="store_true",
                        help="Find loud audio in recordings with no kill-feed matches.")
    parser.add_argument("--show-feed", action="store_true",
                        help="Print the most common OCR rows from each montage recording.")
    parser.add_argument("--audio-offset-ms", type=int, default=0,
                        help="Shift all exported audio tracks. Negative brings audio earlier; positive delays it.")
    parser.add_argument("--radeon-encode", action="store_true",
                        help="Try AMD AMF hardware encoding for exports.")
    parser.add_argument("--radeon-decode", action="store_true",
                        help="Try Radeon D3D11VA decoding for OCR and exports on Windows.")
    args = parser.parse_args()
    preset = load_presets()[args.preset] if args.preset else {}
    if args.feed_region is None:
        args.feed_region = preset.get("feed_region", "58,0,42,30")
    if args.feed_text_mode is None:
        args.feed_text_mode = next((key for key, mode in OCR_MODE_ARGS.items() if mode == preset.get("feed_mode")),
                                   "grayscale")
    is_folder = args.recording is not None and args.recording.is_dir()
    if args.files is not None and (args.files < 1 or not is_folder):
        parser.error("--files requires a folder and a positive whole number.")
    if args.badge_feed_details and not args.kill_montage:
        parser.error("--badge-feed-details requires --kill-montage.")
    if (args.montage_format or args.mix_mic_track is not None) and not args.kill_montage:
        parser.error("--montage-format and --mix-mic-track require --kill-montage.")
    if args.mix_mic_track is not None and args.mix_mic_track < 1:
        parser.error("--mix-mic-track requires a positive track number.")
    style = MontageStyle(
        transition=next(name for name in TRANSITIONS if name.lower().replace(" ", "-") == args.transition),
        vertical={"off": "Off", "blur": VERTICAL_MODES[1], "crop": VERTICAL_MODES[2]}[args.vertical],
        slowmo_multikills=args.slowmo_multikills, mic_at_kills=args.mic_at_kills)
    if style.active and not args.kill_montage:
        parser.error("--transition, --vertical, --slowmo-multikills and --mic-at-kills require --kill-montage.")
    if args.mic_at_kills and args.mix_mic_track is None:
        parser.error("--mic-at-kills requires --mix-mic-track.")
    if args.recording is None:
        from desktop import launch
        launch()
        return
    try:
        acceleration = resolve_video_acceleration(VideoAcceleration(args.radeon_encode, args.radeon_decode))
        sources = folder_recordings(args.recording) if is_folder else [args.recording]
        if args.from_date or args.to_date:
            sources = filter_recording_dates({path: recording_date(path) for path in sources},
                                             args.from_date, args.to_date)
        if args.files is not None:
            sources = sources[:args.files]
        before = args.before if args.before is not None else (3 if args.kill_montage else 10)
        after = args.after if args.after is not None else (2 if args.kill_montage else 6)
        if args.kill_montage:
            if args.output is None:
                raise ValueError("--kill-montage requires an output folder.")
            fallback_track = (args.mic_track if args.mic_track else "auto") if args.montage_audio_fallback else 0
            badge = args.kill_badge or args.badge_feed_details
            validate_detection(fallback_track, args.threshold, args.player, "", before, after, badge=badge)
            feed = KillFeedOptions(parse_feed_region(args.feed_region), args.any_feed_mention,
                                   fallback_track, args.threshold,
                                   OCR_MODE_ARGS[args.feed_text_mode],
                                   parse_feed_divider(args.feed_divider) if args.feed_divider is not None else None,
                                   badge=badge, badge_feed_details=args.badge_feed_details,
                                   weapon_references=merge_weapon_references(
                                       load_weapon_references(args.settings or settings_path()),
                                       preset.get("weapon_references", ())))
            if args.only_weapon:
                labels = {reference.label.casefold() for reference in feed.weapon_references
                          if reference.mode == feed.mode}
                missing = parse_weapon_filter(",".join(args.only_weapon)) - labels
                if missing:
                    raise ValueError(f"No saved {feed.mode} weapon icon is labelled {', '.join(sorted(missing))}. "
                                     "Save one in Test feed OCR, or pass --settings.")
            results, montage = kill_montage(sources, args.player, before, after, args.output,
                                            audio_offset_ms=args.audio_offset_ms, workers=args.workers,
                                            feed=feed, acceleration=acceleration,
                                            output_format=args.montage_format or "mkv",
                                            mic_mix_track=args.mix_mic_track, style=style,
                                            weapons=parse_weapon_filter(",".join(args.only_weapon)))
            for result in results:
                if args.show_feed and result.badge_samples is not None:
                    print(f"{result.source.name}: KILL badge seen in {result.badge_samples} sample(s).")
                    print(f"  Readable score in {result.badge_score_samples} sample(s).")
                    if result.badge_feed_matches is not None:
                        print(f"  Linked to a kill-feed row: {result.badge_feed_matches} notification(s).")
                if args.show_feed and result.feed_scanned:
                    print(f"{result.source.name}: exact player name seen in {result.feed_mentions} sample(s), "
                          f"{result.feed_matches} matching current rule.")
                    print(f"  Left side: {result.feed_left_mentions}, right side: {result.feed_right_mentions}, "
                          f"side unknown: {result.feed_unknown_mentions} sample(s).")
                    if result.feed_green_mentions is not None:
                        print(f"  Player name marked green in {result.feed_green_mentions} sample(s).")
                    for spelling, count in result.feed_lookalikes.most_common():
                        print(f"  Green OCR look-alike {spelling}: {count} sample(s).")
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
            for result in process_batch(sources, options, args.output, audio_offset_ms=args.audio_offset_ms,
                                        acceleration=acceleration):
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
        clips = scan(args.recording, info, *options, acceleration=acceleration)
        for clip in clips:
            print(f"{clip.start:.2f}–{clip.end:.2f}s  {', '.join(clip.reasons)}")
            if args.output is not None:
                print(export_clip(args.recording, clip, args.output, audio_offset_ms=args.audio_offset_ms,
                                  acceleration=acceleration))
        print(f"{len(clips)} highlight candidate(s).")
    except (OSError, RuntimeError, ValueError, re.error) as error:
        parser.exit(1, f"QuickClip: {error}\n")


if __name__ == "__main__":
    main()
