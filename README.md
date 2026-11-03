# Overview

A place to start collecting QC tools for video and audio automated checks.

| Tool | What it checks |
| --- | --- |
| [`find_offline.py`](#find_offlinepy) | Video: "Media Offline" frames and other isolated single-frame glitches |
| [`check_levels.py`](#check_levelspy) | Audio: integrated loudness, loudness range, and true peak, optionally against a delivery standard |

Both tools are single-file Python scripts that use only the standard library and call `ffmpeg` on your PATH. Both return exit codes, so they can be dropped into a script or pre-delivery checklist.


## find_offline.py

Detects "Media Offline" frames (the solid red slate DaVinci Resolve inserts when it can't find media) and other isolated single-frame glitches in video files, before you encode a DCP or deliver a master.

It uses ffmpeg's `signalstats` filter to measure every frame, then flags frames that look like an offline slate or that differ sharply from both of their neighbors. It reads frame rate, bit depth, codec, and pixel format with ffprobe, so there is nothing to configure per file.

### Requirements

- Python 3.7 or newer (standard library only, no pip packages)
- `ffmpeg` and `ffprobe` on your PATH

Check with:

```
ffmpeg -version
ffprobe -version
```

_Note: your system may require the use of `python3` instead of `python` referenced in examples below._

### Quick start

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

### Example output

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

### Options

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
| `--tail-pop SECS` | Treat a lone outlier frame in the last `SECS` seconds of the file as a likely tail 2-pop. `0` disables. Default `3`. |

Directory scanning picks up these extensions (case-insensitive): `.mov .mp4 .m4v .mxf .mkv .avi .mpg .mpeg .mts .m2ts .webm`. Hidden files (names starting with `.`) are skipped.

### Output files

For each input, two files are written (unless `--temp-stats` is used):

- `<video filename>.signalstats.txt`: the raw per-frame ffmpeg output
- `<video filename>.report.txt`: the same report that was printed to the terminal

The full video filename, including its extension, is part of the name, so `a.mov` and `a.mxf` never collide. With `--stats-dir` and `-r`, files from different subfolders that share a filename will overwrite each other.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | All files clean |
| `1` | At least one suspect frame was found (likely 2-pops do not count) |
| `2` | At least one file failed to process (other files are still processed) |

This makes it easy to use in a script or pre-delivery checklist:

```
python find_offline.py -d ./masters || echo "Check the report before encoding!"
```

### How detection works

Two independent tests are run on every frame. Values are normalized to an 8-bit scale first, so the thresholds behave the same for 8-, 10-, and 12-bit files.

1. **RED**: the frame's average color is strongly red (V > 170, U < 125, saturation > 40). This targets Resolve's offline slate.
2. **OUTLIER**: the frame differs by more than `--outlier` (sum of Y, U, and V average differences) from both the previous and next frames, while those two neighbors are similar to each other (within `--neighbor`). This catches any one-frame flash or glitch, not just red ones, and does not trigger on normal hard cuts.

A frame is reported if either test fires.

**2-pops.** A frame flagged only by the OUTLIER test is reported as a `NOTICE` (`likely 2-POP`) instead of a suspect frame when either:

- its timecode is exactly `HH:59:58:00` (the head pop, two seconds before an hour start), or
- it falls within the last `--tail-pop` seconds of the file (the tail pop).

These notices appear in the report and summary but do not affect the exit code. A frame that also trips the RED test is always a suspect.

### Automatic detection details

- **Frame rate:** from ffprobe's average frame rate (falls back to the nominal rate).
- **Bit depth:** from the pixel format (for example `yuv422p10le` = 10, `yuv420p` = 8), so H.264 profiles such as High 10 are handled automatically.
- **Alpha:** formats like ProRes 4444 have their alpha channel dropped for analysis.
- **Unsupported formats:** RGB, NV12, gray, and similar are converted to YUV 4:4:4 for analysis, and a note is printed.
- **Timecode:** an embedded start timecode is added to reported timecodes. Drop-frame is supported for 29.97 and 59.94. 23.976 material displays timecode at a nominal 24 fps.

### Suggested workflow

Put each stage's output in the same folder and run one command:

```
python find_offline.py -d ./deliverables
```

For example, analyze the 23.976 ProRes master, the 24 fps re-encode, and any DCP review export. A frame that appears only in a later stage shows which step introduced it.

### Tuning

- **False positives on red-heavy scenes:** run the script on a file you know is clean and note which frames get flagged. Raise `--outlier` (for example `60`) to make the outlier test stricter. The RED thresholds are set in the `detect()` function.
- **A real glitch is missed:** open the file's `.signalstats.txt`, look up the Y/U/V values around the known bad frame, and adjust `--outlier` or `--neighbor` accordingly. Use `--reuse` to re-run quickly without re-decoding the video.

### Limitations

- Only the first video stream of each file is analyzed. Audio is ignored.
- The outlier test needs a frame on each side, so it cannot flag the very first or last frame of a file. The RED test still can.
- Two or more consecutive bad frames will not trigger the outlier test, because the neighbors no longer match each other. The RED test will still catch red slates.
- Thresholds were validated on synthetic red frames and on a real Resolve offline frame. Other kinds of damage (partial-frame corruption, for instance) may not be detected.
- Analysis decodes every frame, so long or high-resolution files take time. A progress indicator is shown while it runs.

### Troubleshooting

- **`ffmpeg not found on PATH` / `ffprobe not found on PATH`:** install ffmpeg and make sure both programs are available from your terminal.
- **`no frames parsed from signalstats output`:** ffmpeg could not decode the file. Run `ffmpeg -i yourfile -f null -` to see its error messages.
- **Nothing happens with `--input-directory`:** check the file extensions against the list above. Files with other extensions are skipped.


## check_levels.py

Measures the integrated loudness (LUFS), loudness range (LRA), and true peak (dBTP) of any audio or video file ffmpeg can read, and optionally checks the result against a delivery standard or custom limits. If a file is out of bounds, the script exits with a non-zero code.

It uses ffmpeg's `ebur128` filter (ITU-R BS.1770 / EBU R128 measurement), so there is nothing to configure per file.

### Requirements

- Python 3.7 or newer (standard library only, no pip packages)
- `ffmpeg` on your PATH

### Quick start

```
# Just report the levels
python check_levels.py mix.wav

# Check against a delivery preset
python check_levels.py film.mov --standard netflix

# Check against custom limits: -24 LUFS +/- 2 LU, true peak no higher than -2 dBTP
python check_levels.py spot.wav --target -24 --tolerance 2 --tp-limit -2

# Several files, JSON output
python check_levels.py ep1.mp4 ep2.mp4 --standard ebu-r128 --json

# Show the built-in presets
python check_levels.py --list-standards
```

### Example output

```
film.mov
  Integrated:  -26.1 LUFS   (target -27 +/-2; +0.9 LU)  PASS
  LRA:          14.2 LU
  True peak:    -3.4 dBTP   (limit -2)  PASS

RESULT: PASS  [Netflix 5.1/stereo, DIALOG-GATED (ffmpeg approximates)]
```

With no standard or limits given, the values are reported and the script exits `0`.

### Options

| Option | Description |
| --- | --- |
| `files` | One or more audio or video files to measure. |
| `-s`, `--standard NAME` | Check against a built-in preset (see [Standards](#standards)). |
| `-t`, `--target LUFS` | Target integrated loudness. Overrides the preset's target. |
| `--tolerance LU` | Allowed plus or minus range around the target. Overrides the preset's tolerance. Defaults to `2` if only `--target` is given. Needs `--target` or `--standard`. |
| `--tp-limit dBTP` | True peak ceiling. Overrides the preset's limit. |
| `--channel CH` | Measure loudness on one channel only, as a dialogue-gating approximation. See [Dialogue approximation](#dialogue-approximation---channel). |
| `--stream N` | Which audio stream to measure. Default: the first audio stream. |
| `--json` | Output JSON instead of text. |
| `--list-standards` | Print the built-in presets and exit. |

### Exit codes

The code is a bitmask, so a script can tell what failed:

| Code | Meaning |
| --- | --- |
| `0` | All files pass (or no check was requested) |
| `1` | Loudness out of range on at least one file |
| `2` | True peak over the limit on at least one file |
| `3` | Both of the above |
| `10` | Error: ffmpeg missing, unreadable file, no audio stream, or parse failure. Takes priority if any file errored. |

With several files, the code reflects all of them combined. Files that fail to process do not stop the others from being measured.

```
python check_levels.py master.mov --standard ebu-r128 || echo "Levels out of spec!"
```

### Standards

ffmpeg itself has no named loudness standards, so these presets are a hand-maintained table at the top of `check_levels.py`.

| Name | Target (LUFS) | Tolerance (LU) | True peak max (dBTP) | Notes |
| --- | --- | --- | --- | --- |
| `ebu-r128` | -23 | 0.5 | -1 | Europe broadcast |
| `atsc-a85` | -24 | 2 | -2 | US broadcast (CALM Act) |
| `arib-tr-b32` | -24 | 1 | -1 | Japan broadcast |
| `oztam` | -23 | 1 | -1 | Australia broadcast |
| `netflix` | -27 | 2 | -2 | Dialogue-gated |
| `amazon` | -24 | 2 | -2 | Amazon Prime Video |
| `appletv` | -24 | 2 | -2 | Apple TV+ |
| `disney` | -27 | 2 | -2 | Dialogue-gated |
| `spotify` | -14 | 1 | -1 | Normalization target |
| `youtube` | -14 | 1 | -1 | Normalization reference |
| `apple-music` | -16 | 1 | -1 | Sound Check |
| `podcast` | -16 | 1 | -1 | Common podcast target |

A few aliases are accepted, for example `r128`, `atsc`, `prime`, and `apple-tv`.

**Verify before delivering.** Delivery specs change, and the streaming-video values in particular were not checked against current distributor documents. Confirm them against the distributor's current technical spec, and edit the table if needed. The music and podcast entries are normalization targets, not hard deliverable specs.

### Dialogue approximation (`--channel`)

Netflix, Disney+, and some other specs measure loudness over dialogue only. ffmpeg cannot do dialogue gating, so `--channel` offers an approximation for 5.1 mixes: measure the center channel by itself.

```
python check_levels.py film51.mov --standard netflix --channel C
```

- **Loudness and LRA** come from the chosen channel only, and the pass/fail check against the target uses that number.
- **True peak** is always measured on the full mix, since the ceiling applies to the whole program.
- The **full-mix loudness** is also printed for reference, and the output is labeled as an approximation.
- **Channel names:** `C` (or `FC`), `L`, `R`, `Ls`, `Rs`, `LFE`. A 0-based index such as `--channel 2` also works, which helps with files that have a missing or non-standard layout tag. Standard 5.1 order is L R C LFE Ls Rs, but some pro files differ, so check the layout with `ffprobe` first.

This is not equivalent to Dolby's Dialogue Intelligence. It includes any music or effects in the center channel and misses dialogue panned elsewhere. For a formal delivery, use the distributor's required tool for the final number, and treat this script as a fast pre-check.

### How measurement works

- ffmpeg's `ebur128` filter reports integrated loudness, loudness range, and true peak in a single pass (true peak uses 4x oversampling).
- **LRA** (loudness range) is the spread of short-term loudness across the program, in LU. It has no pass/fail limit in this script.
- **Pass/fail:** loudness passes if it is within the target plus or minus the tolerance. True peak passes if it is at or below the limit.
- Digital silence is reported as `-inf` and fails any loudness check.

### Limitations

- Only one audio stream per file is measured (the first, unless `--stream` is given).
- Whole-program measurement only, with no dialogue gating. See the section above for the `--channel` approximation.
- Preset values are not authoritative. See the note under [Standards](#standards).
- Analysis decodes the full audio of each file, so long files take time.

### Troubleshooting

- **`ffmpeg not found in PATH`:** install ffmpeg and make sure it is available from your terminal.
- **`no audio stream found`:** the file has no audio, or `--stream` points past the last audio stream. Run `ffprobe yourfile` to see what is in it.
- **Error with `--channel`:** the channel name may not exist in the file's layout. Try a 0-based index instead, for example `--channel 2`.
- **`could not find ebur128 summary`:** ffmpeg could not decode the file. Run `ffmpeg -i yourfile -f null -` to see its error messages.
