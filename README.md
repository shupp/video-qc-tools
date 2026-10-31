# Overview

A place to start collecting QC tools for video and audio automated checks. The first tool is find_offline.py.


# find_offline.py

Detects "Media Offline" frames (the solid red slate DaVinci Resolve inserts when it can't find media) and other isolated single-frame glitches in video files, before you encode a DCP or deliver a master.

It uses ffmpeg's `signalstats` filter to measure every frame, then flags frames that look like an offline slate or that differ sharply from both of their neighbors. It reads frame rate, bit depth, codec, and pixel format with ffprobe, so there is nothing to configure per file.

## Requirements

- Python 3.7 or newer (standard library only, no pip packages)
- `ffmpeg` and `ffprobe` on your PATH

Check with:

```
ffmpeg -version
ffprobe -version
```

_Note: your system may require the use of `python3` instead of `python` referenced in examples below._

## Quick start

```
# One file
python find_offline.py master24.mov

# Several files
python find_offline.py master.mov master24.mov

# Every video in a directory
python find_offline.py --input-directory /path/to/masters

# Include subdirectories
python find_offline.py -d /path/to/masters -r
```

## Example output

```
=== master24.mov
  codec prores (HQ), yuv422p10le, 1920x1080, 24.000 fps, 10-bit, start TC 01:00:00:00
  analyzed 172800 frames
  RESULT: 1 suspect frame(s)
    frame    86412  TC 02:00:00:12  [RED, OUTLIER]  Y=52 U=107 V=189
  stats:  qc_stats/master24.mov.signalstats.txt
  report: qc_stats/master24.mov.report.txt
```

Each flagged frame shows its 0-based frame number, its timecode (including the file's embedded start timecode), why it was flagged, and its average Y/U/V values (8-bit scale).

## Options

| Option | Description |
| --- | --- |
| `inputs` | One or more video files to analyze. |
| `-d`, `--input-directory DIR` | Analyze every video file in `DIR`. Can be combined with individual files. |
| `-r`, `--recursive` | With `--input-directory`, also search subdirectories. |
| `--stats-dir DIR` | Where to keep the stats and report files. Created if missing. Default: a `qc_stats` folder next to each video. |
| `--temp-stats` | Use a temporary directory for stats and delete it afterward. No files are kept. |
| `--reuse` | If an up-to-date stats file already exists for a video, skip ffmpeg and re-analyze from it. Useful when tuning thresholds. |
| `--outlier N` | Minimum distance from both neighbors for the outlier test. Default `40`. |
| `--neighbor N` | Maximum distance between the previous and next frames for the outlier test. Default `15`. |

Directory scanning picks up these extensions (case-insensitive): `.mov .mp4 .m4v .mxf .mkv .avi .mpg .mpeg .mts .m2ts .webm`. Hidden files (names starting with `.`) are skipped.

## Output files

For each input, two files are written (unless `--temp-stats` is used):

- `<video filename>.signalstats.txt`: the raw per-frame ffmpeg output
- `<video filename>.report.txt`: the same report that was printed to the terminal

The full video filename, including its extension, is part of the name, so `a.mov` and `a.mxf` never collide. With `--stats-dir` and `-r`, files from different subfolders that share a filename will overwrite each other.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | All files clean |
| `1` | At least one suspect frame was found |
| `2` | At least one file failed to process (other files are still processed) |

This makes it easy to use in a script or pre-delivery checklist:

```
python find_offline.py -d ./masters || echo "Check the report before encoding!"
```

## How detection works

Two independent tests are run on every frame. Values are normalized to an 8-bit scale first, so the thresholds behave the same for 8-, 10-, and 12-bit files.

1. **RED**: the frame's average color is strongly red (V > 170, U < 125, saturation > 40). This targets Resolve's offline slate.
2. **OUTLIER**: the frame differs by more than `--outlier` (sum of Y, U, and V average differences) from both the previous and next frames, while those two neighbors are similar to each other (within `--neighbor`). This catches any one-frame flash or glitch, not just red ones, and does not trigger on normal hard cuts.

A frame is reported if either test fires.

## Automatic detection details

- **Frame rate:** from ffprobe's average frame rate (falls back to the nominal rate).
- **Bit depth:** from the pixel format (for example `yuv422p10le` = 10, `yuv420p` = 8), so H.264 profiles such as High 10 are handled automatically.
- **Alpha:** formats like ProRes 4444 have their alpha channel dropped for analysis.
- **Unsupported formats:** RGB, NV12, gray, and similar are converted to YUV 4:4:4 for analysis, and a note is printed.
- **Timecode:** an embedded start timecode is added to reported timecodes. Drop-frame is supported for 29.97 and 59.94. 23.976 material displays timecode at a nominal 24 fps.

## Suggested workflow

Put each stage's output in the same folder and run one command:

```
python find_offline.py -d ./deliverables
```

For example, analyze the 23.976 ProRes master, the 24 fps re-encode, and any DCP review export. A frame that appears only in a later stage shows which step introduced it.

## Tuning

- **False positives on red-heavy scenes:** run the script on a file you know is clean and note which frames get flagged. Raise `--outlier` (for example `60`) to make the outlier test stricter. The RED thresholds are set in the `detect()` function.
- **A real glitch is missed:** open the file's `.signalstats.txt`, look up the Y/U/V values around the known bad frame, and adjust `--outlier` or `--neighbor` accordingly. Use `--reuse` to re-run quickly without re-decoding the video.

## Limitations

- Only the first video stream of each file is analyzed. Audio is ignored.
- The outlier test needs a frame on each side, so it cannot flag the very first or last frame of a file. The RED test still can.
- Two or more consecutive bad frames will not trigger the outlier test, because the neighbors no longer match each other. The RED test will still catch red slates.
- Thresholds were validated on synthetic red frames and on a real Resolve offline frame. Other kinds of damage (partial-frame corruption, for instance) may not be detected.
- Analysis decodes every frame, so long or high-resolution files take time. A progress indicator is shown while it runs.

## Troubleshooting

- **`ffmpeg not found on PATH` / `ffprobe not found on PATH`:** install ffmpeg and make sure both programs are available from your terminal.
- **`no frames parsed from signalstats output`:** ffmpeg could not decode the file. Run `ffmpeg -i yourfile -f null -` to see its error messages.
- **Nothing happens with `--input-directory`:** check the file extensions against the list above. Files with other extensions are skipped.
