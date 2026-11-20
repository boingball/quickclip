# QuickClip

QuickClip finds highlight candidates in local Battlefield 6 recordings made with OBS. It scans an isolated microphone track for loud reactions and can look for your player name in the kill feed or score text near the centre of the frame. Review the results and export the moments you want to keep.

## Run on Windows

1. Install Python 3.10 or later with Tcl/Tk enabled.
2. Install an [FFmpeg build](https://ffmpeg.org/download.html) with `ffmpeg`, `ffprobe`, and the H.264 encoder `libx264`. Add its `bin` folder to PATH. Include `ffplay` to preview clips from the app.
3. For on-screen detection, install [Tesseract](https://tesseract-ocr.github.io/tessdoc/Installation.html) with English language data and add its installation folder to PATH.
4. Open PowerShell in this folder and run `py quickclip.py`.

On Linux, use `python3 quickclip.py` with the same tools on PATH and your distribution's Tkinter package. No pip dependencies are needed.

## Find highlights

1. Open an OBS recording. Choose the isolated **Microphone track** from the list. Track numbers are the saved audio streams in file order. If OBS recorded only tracks 1 and 4, for example, the app lists them as 1 and 2.
2. Start with a **Mic threshold** of **−18 dBFS**. Raise it toward zero if ordinary speech triggers clips. Lower it if OBS compression or gain makes your reactions quieter.
3. To enable OCR, enter your exact in-game name, turn on **Detect score popups**, or both. The feed scan uses the upper-right of the image and the score scan uses the centre. The default score pattern looks for text such as `+150` or `150 XP`. You can enter a custom regular expression for the text your HUD shows.
4. Set how many seconds to keep before and after each cue, then choose **Find highlights**. Overlapping windows merge into one candidate and show all the cues that contributed.
5. Select one or more rows and choose **Export selected**. Each MKV keeps every source audio stream as lossless FLAC, with stream titles where present. H.264 encoding allows cuts between source keyframes. The source recording stays in place.

Keep your mic on its own recording track in OBS so loud gameplay and voice chat do not drive the mic detector. Kill-feed OCR flags any newly visible line containing your name, including deaths or assists. Score scanning can pick up other matching HUD text, and small or blurred text can be missed. Start with a short recording to check how the defaults fit your footage. Mic-only scanning is faster for long sessions.

## Process a folder

1. Click **Open folder** and select your recordings folder. QuickClip scans supported video files directly inside it. The microphone track list comes from the first readable recording, so use the same mic track position throughout the batch.
2. Click **Output folder** to choose where clips go, or use the default `Highlights` subfolder. The app creates it when exporting.
3. Click **Scan and export all** to process the batch automatically, or **Find highlights** to review candidates first. The app shows the current file and keeps going if a recording fails. The results list identifies each candidate's source, and any errors appear at the end.

For a large first batch, try mic-only detection and **Find highlights** on a few recordings to check your track and threshold before enabling OCR or exporting everything. Running an export again can create additional copies with numbered filenames.

## PowerShell and terminal use

List the audio streams:

```powershell
py quickclip.py "D:\OBS\match.mkv"
```

Scan and list candidates:

```powershell
py quickclip.py "D:\OBS\match.mkv" --mic-track 2 --threshold -18 --player "YourName" --score
```

Add `--output "D:\OBS\Highlights"` to export all candidates. Use `--help` for the full option list.

Pass a folder instead of a recording to process it in one command:

```powershell
py quickclip.py "D:\OBS\Recordings" --mic-track 2 --threshold -18 --output "D:\OBS\Highlights"
```

Omit `--output` to list candidates without exporting. A batch with errors still processes the other recordings and returns a nonzero exit code.
