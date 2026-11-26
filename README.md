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

A recording that was cut off while being written, for example an interrupted replay save, has no duration or seek index. QuickClip reads its length from the last video frame instead, which takes a few seconds.

For a large first batch, try mic-only detection and **Find highlights** on a few recordings to check your track and threshold before enabling OCR or exporting everything. Running an export again can create additional copies with numbered filenames.

## Make a kill montage

For Battlefield recordings with the **KILL** notification, set **Montage cues** to **KILL badge**. QuickClip looks for either the boxed label or a skull immediately beside a readable score in the lower part of the screen. It uses the original luminance around the score to check for faint skulls. To check a frame first, open **Set kill-feed area**, enter the frame time, and click **Test KILL badge**. The preview outlines the recognized score in blue and the skull search area in yellow. Choose **Both** to also scan the kill feed. Overlapping matches merge into one clip.

If the score is readable and the skull is visible but the test finds no match, draw a tight box around the skull in the zoomed **Skull reference** view and click **Save skull reference**. The preview checks the saved template immediately, and desktop montage scans use it for later recordings with the same HUD. **Clear reference** restores the built-in check on its own.

**OCR readout** reports how many samples had a readable score and whether the desktop scan used a saved skull reference. Several matching samples can belong to one notification and produce a single clip.

1. Open a recording or folder, enter your in-game name, and set the seconds before and after each kill. Set **Montage file limit** to `5` for a short trial, or leave it blank to scan the whole folder.
2. Click **Find montage clips**. QuickClip runs Tesseract only on the kill-feed crop. It looks for the gap between names separately in each row and counts your name on the killer side. It merges overlapping moments from the same recording. Up to four recordings scan concurrently by default, and results appear as each file finishes. Adjust **Montage scan workers** to change how many run at once.
While a montage scan runs, **Stop scan** stops it after the current frames. Finished replays are kept, and **Resume replay scan** picks up the rest later. Closing the window during a scan saves it first; close again to quit at once, which stops every FFmpeg and Tesseract process QuickClip started.

3. Select a row and click **Play clip** to preview it in FFplay. FFmpeg first shrinks the clip to 720p and converts HDR to normal colours, using the Radeon decoder when **Radeon hardware decoding** is on, so 4K replays fit on screen and play smoothly. Click its **Keep** checkbox or press **Space** on the focused row to toggle it. All candidates are checked initially.
4. Choose the output folder and set **Montage format** to MKV or MP4. Click **Build montage** to join the checked clips in filename order.

MP4 copies the prepared video without another encode and converts each audio track to AAC for playback compatibility. MKV keeps the prepared audio tracks as-is. Enter your audio track number in **Mix mic track (optional)** to add a default stereo game-and-mic mix while keeping the separate tracks. QuickClip uses the track named Game or Game Audio for the mix when available, or the first other track. Recordings with a missing track use silence for that track. Leave the field blank to export the original track lineup.

Select a result to see three frames and the **Play clip** button in the right-hand preview panel. Drag the divider to resize the panel, and scroll through its frames if needed. Settings collapse automatically in short windows. Use **Show settings** to open them again. The **Audio** column lists the source track count or **No audio**.

**Pick dates** shows the oldest and newest recording dates and offers **Today**, **Yesterday**, **This week**, and custom calendar bounds. QuickClip uses a `YYYY-MM-DD` date in the filename when available, then falls back to the file's modified date. The date filter applies before the file limit. The CLI equivalents are `--from-date` and `--to-date`.

For weapon hints, open **Test feed OCR** on a frame where one of your kills shows the weapon clearly. Draw a tight box around the weapon icon, enter a label such as **Rocket**, and click **Save weapon icon**. The icon needs a readable name just before it; the victim's name doesn't need to be readable. Weapon icons are saved per OCR treatment, so save them with the treatment you scan with. During a scan, QuickClip looks for the saved icons after your name on each new kill line, for up to 5 seconds after the kill, because the icon is often clearest a moment after the line appears. The label appears in **Weapon / target**. **Both** lets the montage use this alongside badge detection. Lower HUD text such as **VEHICLE DESTROYED** can provide a target hint. A weapon icon alone cannot identify a helicopter.

To make a montage of one weapon, such as rockets only, enter its label in **Only weapons**. Awards count too: enter `Headshot` for a montage of headshot kills found by the **KILL badge** or **Both** cue. Separate several labels with commas, such as `Rocket, Sniper`. After **Find montage clips**, only kills with a matching weapon start ticked. Kills with other or unrecognized weapons stay in the list, unticked, so you can tick any that QuickClip missed. On sample Battlefield 6 replays, a single saved rocket icon labelled 3 of 4 rocket kills and none of the other kills. The missed kill had a very faint icon over bright sky. On the command line, use `--only-weapon Rocket`. The CLI reads saved icons from the desktop app's settings, or from a file given with `--settings`.

Use **Badge + feed details** to scan the lower HUD for kills and read the normal top-right feed only around each badge hit. The icon reference follows the gap between names as it moves. QuickClip adds a victim or weapon only when it can link a single feed row to the popup's victim name or to a newly visible row with your name on the killer side. The CLI option is `--kill-montage --badge-feed-details`.

A **HEADSHOT** badge counts as a kill, and adds **HEADSHOT** to **Awards**. **DOUBLE KILL**, **QUAD KILL** or **KILL x3** add a kill count, so **Slow motion on multi-kills** applies. Badges that are not kills are ignored even when their icon looks like a skull: assists (damage, suppression, spot), captures, completed orders, revives, M-COM and Gauntlet objectives. **VEHICLE DESTROYED** is read for up to 1.5 seconds after the kill, since the game adds badges one after another.

Badge detection also checks the award banner above the kill notification. The **Awards** column lists recognized titles such as **FLYING ACE**, **BALLISTIC**, and readable titles under **NEW BEST**. **BALLISTIC** also adds a helicopter hint in **Weapon / target**.

### Montage style: transitions, TikTok, slow motion, and mic at kills

These options make a finished, share-ready montage. With all of them off, QuickClip joins clips exactly as above.

- **Montage transition**: **Flip** folds each clip to a vertical line and unfolds the next one like a turning card. **Whip pan** slides the next clip in with motion blur. **Zoom punch** zooms into the cut. **Flash** cuts through white. **Pixelate** and **Slide** are subtler. **Random** saves its transition order for the job and uses every effect once before repeating. Transitions default to 0.4 seconds and may shorten for very short clips. Styled audio crossfades with the picture.
- **Vertical for TikTok**: makes a 1080×1920 montage for TikTok, YouTube Shorts, and Reels. **9:16 blurred fill** shows the whole picture over a blurred, zoomed copy of itself. **9:16 centre crop** fills the screen with the middle of the picture, which includes the crosshair and the KILL badge but not the kill feed or minimap. The file is named `kill-montage-vertical`.
- **Slow motion on multi-kills**: treats two or more kills as one event while successive kills are at most three seconds apart. Slow motion starts up to 0.5 seconds before the first kill and ends 0.85 seconds after the last. Short intermediate-speed steps ease in and out when there is room, and the audio follows the same timeline at its original pitch.
- **Mic only around kills**: mixes the track in **Mix mic track** into the game audio from 1 second before each kill to 2.5 seconds after, and keeps it silent the rest of the time. Without this option, the mic plays throughout.

A styled montage has one audio track (game, plus your mic when **Mix mic track** is set), because TikTok, YouTube, and phones play only the first track. HDR recordings, such as Battlefield 6's 10-bit HLG captures, are tone mapped to standard range so colours look right on phones and social sites. That needs an FFmpeg build with `zscale`, which the common Windows builds include. Styled HDR exports require this filter. Unstyled montages with compatible HDR recordings retain HDR. If an unstyled montage needs to combine different HDR and SDR colour formats, QuickClip converts the clips to a common SDR format.

Styling re-encodes the picture once. Each transition is encoded separately and the pieces are joined without another encode, so long montages don't take disproportionately longer. **Radeon hardware encoding** speeds this up a lot. Slow motion and mic-at-kills use the kill times from the scan, so they need clips found by **Find montage clips**, not clips added by hand.

Click **Pause montage** to stop after the current clip or conversion finishes. QuickClip saves completed work in the output folder. You can change the Radeon options, then click **Resume montage**, including after reopening QuickClip. Resume uses the selected clips, output format, mic mix, montage style, and audio offset from the saved build. It uses the current Radeon options for work that remains. QuickClip checks cached pieces for the expected streams, duration, audio timing, and successful decoding. It saves a new intermediate only after those checks pass. A new job with changed styling can reuse matching extracts and rendered pieces from a suspended job in the same output folder when the filesystem supports hard links. If a selected clip fails, the job stays available to resume and assembly stops before publishing an incomplete montage. The diagnostic includes the replay, clip start time, and processing stage. Find the FFmpeg argument lists and errors in `.quickclip-montage-resume/<job>/ffmpeg.log` in the output folder.

To run the montage regression suite from the project folder, use `py -m unittest discover -s tests` on Windows or `python3 -m unittest discover -s tests` on Linux. The tests use small synthetic recordings and require FFmpeg with libx264 and zscale. They leave your recordings and saved jobs in place.

QuickClip saves the replay list, completed scans and your **Keep** choices as you go. On reopening the same input, it restores the review. Click **Resume replay scan** to scan the remaining recordings using the saved detection settings. A recording with a changed file size or modification time is scanned again.

On Windows, tick **Shut down PC after montage** for an unattended build. QuickClip starts a 60-second **Cancel shutdown** countdown only after successfully saving the montage with no clip errors. The option starts unchecked each time you open QuickClip.

Open **OCR readout** to see possible names and readable rows from each recording. If those rows look wrong, open **Set kill-feed area**, load a frame at a known kill, and draw a tight box around the complete feed. Choose **Colored HUD** for red, blue, or green lettering, **Bright text** for light lettering, or **Grayscale** for an unfiltered comparison. Click **Test feed OCR** to see the processed crop and Tesseract's readings for that frame before saving or scanning the folder. The numbers next to the words are Tesseract confidence scores. **Match name anywhere in feed** is a looser check that can also include deaths and assists.

In **Set kill-feed area**, include the entire feed with space for long names on both sides. **Test feed OCR** shows the detected gaps and labels each word left, right, or unknown. **OCR readout** counts all three. When the other name in a row is too faint to read, QuickClip uses the feed's right edge instead: a killer's name is followed by the weapon icon and the victim, so it ends well short of the edge, while a victim's name reaches it. For a feed with a fixed split, enable **Use fixed divider** to use a single vertical line.

The **Recording** list in **Set kill-feed area** lets you pick any file in the open folder. Use **Play recording** to open it in FFplay from the entered time. In FFplay, use the arrow keys to seek and **Space** to pause when the kill is visible. Return to QuickClip and click **Use playback time**, then **Test feed OCR**. You can adjust the frame time by hand if you want a nearby frame.

Each kill is reported once, when another line with your name appears on the killer side. Lines that stay up for several seconds, or briefly drop out of OCR, do not start new clips. Multi-kills shown together become one clip.

For Battlefield 6, pick **Battlefield 6** under **Game preset** and click **Apply**. This sets the kill-feed area for the default HUD, **Local contrast** text, and the **Kill feed** montage cue, and adds a built-in **Rocket** weapon icon. Your own saved icons are kept, and a saved icon with the same label takes priority. On the command line, use `--preset battlefield6`; `--feed-region` and `--feed-text-mode` still override it. Presets live in the `presets` folder as JSON, so more games or weapon icons can be added there.

Kills are told apart by counting your lines in the feed and, when OCR can read it, by the victim's name. A faint feed may be read only one line at a time, so a victim read for the first time also counts as a new kill. That keeps quick double kills apart. If a kill was already reported without a readable victim moments before, the name is matched to that kill instead of counting twice.

Older recordings may not show the whole feed. A capture cropped on the right cuts the feed off after your name, and a lower resolution such as 1536x1024 leaves the text small and faint. For those, choose the **Both** montage cue, so the **KILL** badge in the middle of the screen catches kills the feed misses.

If you set things up by hand, choose **Local contrast**. The game draws your own name in near-white with a ▸ marker, not in your squad's green, and the feed is translucent: over bright sky your name is only about 15 brightness levels from the background, and mostly visible by its outline. **Local contrast** stretches each pixel between the darkest and brightest values around it, which keeps those names readable without depending on color. The command-line equivalent is `--feed-text-mode local`. Your player name is matched without regard to case, and with one common OCR substitution such as `0` for `O`. On sample BF6 replays it found 11 of 11 kills with no false clips, against 9 of 11 for **Grayscale** and none for **Green player**.

HDR recordings (for example BF6's 10-bit HLG replays) are converted to 8 bits before OCR, without tone mapping. Tone mapping made no difference to OCR in testing.

If your name is always green, try **Green player**. QuickClip requires the matching name to be green and keeps the other colored names visible for the killer-side check. For names of at least six letters or digits, every mode also accepts one common OCR substitution, such as `l` for `i` or `o` for `0`. **OCR readout** lists those spellings separately for review. **Test feed OCR** marks the words it classified as green. The command-line equivalent is `--feed-text-mode green`.

For washed-out lettering, try **Faint text** in **Test feed OCR** first. It boosts small differences in brightness and color around the letters. It works on light and dark names in the same row without requiring a green hue. Save the area to apply the treatment to your montage scan. The command-line equivalent is `--feed-text-mode faint`.

Enable **Use loud audio if no kill matches** for older recordings. This uses the mic threshold and selected track. Auto tries an isolated mic first and uses Track 1 when there is no clear mic. With only a mixed track, loud game audio can also trigger candidates.

QuickClip joins compatible clips directly. With mixed formats it re-encodes them to the first clip's resolution, fits each picture without stretching, and uses 48 kHz audio. It keeps the audio tracks, aligns shared track titles where possible, and fills missing tracks with silence. The export audio offset still applies.

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

Use `--show-feed` to show OCR rows and `--feed-region 58,0,42,30` to set the crop as left, top, width, and height percentages. Use `--feed-divider 50` to override per-row detection with a fixed split halfway across the crop. Use `--feed-text-mode colored` to isolate saturated lettering, `--feed-text-mode local` for translucent feeds such as Battlefield 6, `--any-feed-mention` for looser matching, and `--montage-audio-fallback` to check loud audio when a recording has no kill matches. For a styled montage, add `--transition flip` (or `whip-pan`, `zoom-punch`, `flash`, `pixelate`, `slide`, `random`), `--vertical blur` or `--vertical crop`, `--slowmo-multikills`, and `--mic-at-kills` with `--mix-mic-track N`.

For a badge-only montage, pass `--kill-montage --kill-badge --output "F:\QuickClip-Montage"`. Add `--player boingball` to scan both cues.

Add `--montage-format mp4` to save an MP4 and `--mix-mic-track 2` if your mic is on audio track 2.

If all audio tracks play late in an exported clip, try `--audio-offset-ms -1000` to bring them one second earlier. The desktop app has the same **Export audio offset (ms)** setting. Positive values delay audio. The offset applies to all tracks during export and defaults to zero.

## Radeon acceleration on Windows

Enable **Radeon hardware encoding** to try FFmpeg's AMD AMF H.264 encoder (`h264_amf`) for clips and montages. Enable **Radeon hardware decoding** to try D3D11VA on an AMD adapter when reading video for scans or exports. The two settings work independently. Tesseract OCR still runs on the CPU.

Use an FFmpeg build that lists `h264_amf` in `ffmpeg -encoders` and `d3d11va` in `ffmpeg -hwaccels`, with your Radeon driver installed. QuickClip falls back to CPU processing if a feature is missing or the hardware attempt fails.

During exports, QuickClip displays the active encoder, frames written, output time, and speed as FFmpeg reports them. Mixed-format montages first export the clips, then convert them to a common format before joining them. The join step shows its own percentage and reaches 100% when the montage is saved.

The command-line equivalents are `--radeon-encode` and `--radeon-decode`.
