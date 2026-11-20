# QuickClip

QuickClip finds highlight candidates in local Battlefield 6 recordings made with OBS. It scans an isolated microphone track for loud reactions and can look for your player name in the kill feed or score text near the centre of the frame. Review the results and export the moments you want to keep.

## Run on Windows

1. Install Python 3.10 or later with Tcl/Tk enabled.
2. Install an [FFmpeg build](https://ffmpeg.org/download.html) with `ffmpeg`, `ffprobe`, and the H.264 encoder `libx264`. Add its `bin` folder to PATH. Include `ffplay` to preview clips from the app.
3. For on-screen detection, install [Tesseract](https://tesseract-ocr.github.io/tessdoc/Installation.html) with English language data and add its installation folder to PATH.
4. Open PowerShell in this folder and run `py quickclip.py`.

On Linux, use `python3 quickclip.py` with the same tools on PATH and your distribution's Tkinter package. No pip dependencies are needed.

## Find highlights

1. Open an OBS recording and leave **Microphone track** on **Auto**, or choose the isolated mic track manually. Track numbers are the saved audio streams in file order. If OBS recorded only tracks 1 and 4, for example, the app lists them as 1 and 2.
2. Start with a **Mic threshold** of **−18 dBFS**. Raise it toward zero if ordinary speech triggers clips. Lower it if OBS compression or gain makes your reactions quieter.
3. To enable OCR, enter your exact in-game name, turn on **Detect score popups**, or both. The feed scan uses the upper-right of the image and the score scan uses the centre. The default score pattern looks for text such as `+150` or `150 XP`. You can enter a custom regular expression for the text your HUD shows.
4. Set how many seconds to keep before and after each cue, then choose **Find highlights**. Overlapping windows merge into one candidate and show all the cues that contributed.
5. Select one or more rows and choose **Export selected**. Each MKV keeps every source audio stream as lossless FLAC, with stream titles where present. H.264 encoding allows cuts between source keyframes. The source recording stays in place.

Keep your mic on its own recording track in OBS so loud gameplay and voice chat do not drive the mic detector. Kill-feed OCR flags any newly visible line containing your name, including deaths or assists. Score scanning can pick up other matching HUD text, and small or blurred text can be missed. Start with a short recording to check how the defaults fit your footage. Mic-only scanning is faster for long sessions.

Auto looks for a track named **Mic** or **Microphone** first. With generic names such as `Track1`, it checks each track for quiet gaps and audible bursts. If your friend's track has a similar pattern, use your media player's audio-track menu to identify yours and select that number manually. Auto checks each recording independently.

## Process a folder

1. Click **Open folder** and select your recordings folder. QuickClip scans supported video files directly inside it. If you choose a mic track manually, use the same mic track position throughout the batch. The track list comes from the first readable recording.
2. Click **Output folder** to choose where clips go, or use the default `Highlights` subfolder. The app creates it when exporting.
3. Click **Scan and export all** to process the batch automatically, or **Find highlights** to review candidates first. The app shows the current file and keeps going if a recording fails. The results list identifies each candidate's source, and any errors appear at the end.

For a large first batch, try mic-only detection and **Find highlights** on a few recordings to check your track and threshold before enabling OCR or exporting everything. Running an export again can create additional copies with numbered filenames.

## Make a kill montage

1. Open a recording or folder, enter your in-game name, and set the seconds before and after each kill. Set **Montage file limit** to `5` for a short trial, or leave it blank to scan the whole folder.
2. Click **Find montage clips**. QuickClip runs Tesseract only on the kill-feed crop. It looks for the gap between names separately in each row and counts your name on the killer side. It merges overlapping moments from the same recording. Up to four recordings scan concurrently by default, and results appear as each file finishes. Adjust **Montage scan workers** to change how many run at once.
3. Select a row and click **Play clip** to preview it in FFplay. Click its **Keep** checkbox or press **Space** on the focused row to toggle it. All candidates are checked initially.
4. Choose the output folder and click **Build montage** to join the checked clips into one MKV in filename order.

Open **OCR readout** to see possible names and readable rows from each recording. If those rows look wrong, open **Set kill-feed area**, load a frame at a known kill, and draw a tight box around the complete feed. Choose **Colored HUD** for red, blue, or green lettering, **Bright text** for light lettering, or **Grayscale** for an unfiltered comparison. Click **Test OCR** to see the processed crop and Tesseract's readings for that frame before saving or scanning the folder. The numbers next to the words are Tesseract confidence scores. **Match name anywhere in feed** is a looser check that can also include deaths and assists.

In **Set kill-feed area**, include the entire feed with space for long names on both sides. **Test OCR** shows the detected gaps and labels each word left, right, or unknown. **OCR readout** counts all three. A lone readable name has an unknown side in automatic mode. Use **Match name anywhere in feed** to include those moments for manual review. For a feed with a fixed split, enable **Use fixed divider** to use a single vertical line.

If your name is always green, try **Green player**. QuickClip requires the matching name to be green and keeps the other colored names visible for the killer-side check. **Test OCR** marks the words it classified as green. The command-line equivalent is `--feed-text-mode green`.

Enable **Use loud audio if no kill matches** for older recordings. This uses the mic threshold and selected track. Auto tries an isolated mic first and uses Track 1 when there is no clear mic. With only a mixed track, loud game audio can also trigger candidates.

Use recordings with compatible video and audio formats. The montage preserves all audio tracks and uses your export audio offset. When mixing a single-track recording with a compatible multitrack recording, QuickClip treats the single track as the main track and fills the missing tracks with silence. Other incompatible layouts need separate montages.

QuickClip remembers your last input, output folder, and settings. On Windows it stores them in `%APPDATA%\QuickClip\settings.json`.

## PowerShell and terminal use

List the audio streams, automatically identify the mic, and show highlight candidates:

```powershell
py quickclip.py "D:\OBS\match.mkv"
```

Use `--mic-track 2` to override the choice. When enabling OCR on the command line, add `--mic-track auto` to enable both detectors.

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

To make a montage from the kill feed, use your in-game name:

```powershell
py quickclip.py "F:\Raw-Stream" --player boingball --kill-montage --output "F:\QuickClip-Montage"
```

The command uses three seconds before and two after each match by default. Override them with `--before` and `--after`. Use `--audio-offset-ms 1000` if the recordings need that sync adjustment.

Add `--files 5` to try the first five recordings in filename order. This limits the number of source files scanned. Each recording can still yield multiple highlights.
Use `--workers 2` to scan two recordings at a time, or `--workers 1` to scan sequentially.

Use `--show-feed` to show OCR rows and `--feed-region 58,0,42,30` to set the crop as left, top, width, and height percentages. Use `--feed-divider 50` to override per-row detection with a fixed split halfway across the crop. Use `--feed-text-mode colored` to isolate saturated lettering, `--any-feed-mention` for looser matching, and `--montage-audio-fallback` to check loud audio when a recording has no kill matches.

If all audio tracks play late in an exported clip, try `--audio-offset-ms -1000` to bring them one second earlier. The desktop app has the same **Export audio offset (ms)** setting. Positive values delay audio. The offset applies to all tracks during export and defaults to zero.
