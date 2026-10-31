#!/usr/bin/env python3
"""
find_offline.py - detect "Media Offline" (red slate) frames and isolated
single-frame glitches in video files, using ffmpeg's signalstats filter.

Single file:
    python find_offline.py master24.mov

Several files:
    python find_offline.py a.mov b.mov

Whole directory (add -r to recurse):
    python find_offline.py --input-directory /path/to/masters

FPS, bit depth, codec, profile and pixel format are read with ffprobe.
For each input a stats file named "<filename>.signalstats.txt" and a
"<filename>.report.txt" are kept in a qc_stats folder next to the video
(override with --stats-dir, or use --temp-stats to discard them).

Requires ffmpeg and ffprobe on the PATH.
Exit code: 0 = clean, 1 = suspect frames found, 2 = a file failed to process.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".mxf", ".mkv", ".avi", ".mpg",
              ".mpeg", ".mts", ".m2ts", ".webm"}
KEYS = ("YAVG", "UAVG", "VAVG", "SATAVG")
FRAME_RE = re.compile(r"^frame:(\d+)")
KV_RE = re.compile(r"^lavfi\.signalstats\.(\w+)=(.+)$")
# pixel formats signalstats accepts natively (alpha is stripped first)
SUPPORTED_RE = re.compile(r"yuvj?4(?:20|22|44|40|11|10)p(?:\d+(?:le|be))?")


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------
def parse_rate(s):
    try:
        f = Fraction(s)
    except (ValueError, ZeroDivisionError, TypeError):
        return None
    return f if f > 0 else None


def probe(path):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries",
        "stream=codec_name,profile,pix_fmt,width,height,r_frame_rate,"
        "avg_frame_rate,nb_frames,duration,bits_per_raw_sample"
        ":stream_tags=timecode:format=duration:format_tags=timecode",
        "-of", "json", str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr.strip()}")
    data = json.loads(r.stdout or "{}")
    if not data.get("streams"):
        raise RuntimeError("no video stream found")
    st = data["streams"][0]
    fmt = data.get("format", {})

    fps = parse_rate(st.get("avg_frame_rate")) or parse_rate(st.get("r_frame_rate"))
    if fps is None:
        raise RuntimeError("could not determine frame rate")

    pix_fmt = st.get("pix_fmt") or ""
    native_depth = depth_from_pix_fmt(pix_fmt, st.get("bits_per_raw_sample"))

    # Decide whether a format conversion is needed for signalstats
    base = pix_fmt.replace("yuva", "yuv")
    if SUPPORTED_RE.fullmatch(base):
        vf_format = base if base != pix_fmt else None  # drop alpha only
        bits = native_depth
    else:
        bits = native_depth if native_depth in (8, 9, 10, 12, 14, 16) else 10
        vf_format = "yuv444p" if bits == 8 else f"yuv444p{bits}le"

    # frame count (for progress only)
    total = None
    nb = st.get("nb_frames")
    if nb and nb.isdigit():
        total = int(nb)
    else:
        dur = st.get("duration") or fmt.get("duration")
        try:
            total = int(float(dur) * float(fps))
        except (TypeError, ValueError):
            pass

    tags = st.get("tags", {}) or {}
    ftags = fmt.get("tags", {}) or {}
    start_tc = tags.get("timecode") or ftags.get("timecode")

    return {
        "codec": st.get("codec_name", "?"),
        "profile": st.get("profile", ""),
        "pix_fmt": pix_fmt,
        "width": st.get("width"),
        "height": st.get("height"),
        "fps": fps,
        "bits": bits,
        "vf_format": vf_format,
        "total": total,
        "start_tc": start_tc,
    }


def depth_from_pix_fmt(pix_fmt, raw_bits=None):
    m = re.search(r"(\d+)(?:le|be)$", pix_fmt)
    if m:
        return int(m.group(1))
    if raw_bits and str(raw_bits).isdigit():
        return int(raw_bits)
    return 8


# --------------------------------------------------------------------------
# Timecode helpers (supports non-drop and 29.97/59.94 drop-frame)
# --------------------------------------------------------------------------
def drop_count(nominal):
    return {30: 2, 60: 4}.get(nominal, 0)


def tc_to_frames(tc, nominal):
    try:
        h, m, s, f = (int(x) for x in re.split(r"[:;.]", tc))
    except ValueError:
        return 0, False
    total = ((h * 60 + m) * 60 + s) * nominal + f
    drop = ";" in tc and drop_count(nominal) > 0
    if drop:
        d = drop_count(nominal)
        mins = h * 60 + m
        total -= d * (mins - mins // 10)
    return total, drop


def frames_to_tc(n, nominal, drop):
    sep = ";" if drop else ":"
    if drop:
        d = drop_count(nominal)
        fpm = nominal * 60 - d
        fp10 = fpm * 10 + d
        blocks, rem = divmod(n, fp10)
        n += 9 * d * blocks + (d * ((rem - d) // fpm) if rem > d else 0)
    ff = n % nominal
    s = n // nominal
    return f"{s // 3600:02d}:{(s // 60) % 60:02d}:{s % 60:02d}{sep}{ff:02d}"


# --------------------------------------------------------------------------
# ffmpeg signalstats run
# --------------------------------------------------------------------------
def run_signalstats(path, info, stats_path):
    vf = ""
    if info["vf_format"]:
        vf += f"format={info['vf_format']},"
    vf += "signalstats,metadata=print:file=-"
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats",
        "-i", str(path), "-map", "0:v:0", "-vf", vf,
        "-an", "-sn", "-dn", "-f", "null", "-",
    ]
    total = info["total"]
    count = 0
    with tempfile.TemporaryFile("w+") as errf, \
            open(stats_path, "w", encoding="utf-8") as out:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                                text=True, errors="replace")
        for line in proc.stdout:
            out.write(line)
            if line.startswith("frame:"):
                count += 1
                if count % 500 == 0:
                    if total:
                        msg = f"  analyzing... {count}/{total} ({100 * count / total:.0f}%)"
                    else:
                        msg = f"  analyzing... {count} frames"
                    print(msg, end="\r", flush=True)
        proc.wait()
        errf.seek(0)
        err = errf.read().strip()
    print(" " * 60, end="\r")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (exit {proc.returncode}): {err[-500:]}")
    return count


def parse_stats(path):
    frames = []
    cur = None
    with open(path, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            m = FRAME_RE.match(line)
            if m:
                cur = {"frame": int(m.group(1))}
                frames.append(cur)
                continue
            m = KV_RE.match(line)
            if m and cur is not None and m.group(1) in KEYS:
                try:
                    cur[m.group(1)] = float(m.group(2))
                except ValueError:
                    pass
    return frames


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------
def detect(frames, bits, outlier, neighbor):
    scale = 2 ** (bits - 8)  # normalize to 8-bit scale
    for fr in frames:
        for k in KEYS:
            if k in fr:
                fr[k] /= scale

    def dist(a, b):
        return sum(abs(a.get(k, 0) - b.get(k, 0)) for k in ("YAVG", "UAVG", "VAVG"))

    flagged = {}
    for i, fr in enumerate(frames):
        # Test 1: strongly red frame (high V, low U)
        if (fr.get("VAVG", 0) > 170 and fr.get("UAVG", 255) < 125
                and fr.get("SATAVG", 0) > 40):
            flagged.setdefault(i, []).append("RED")
        # Test 2: isolated outlier vs. both neighbors
        if 0 < i < len(frames) - 1:
            p, n = frames[i - 1], frames[i + 1]
            if (dist(fr, p) > outlier and dist(fr, n) > outlier
                    and dist(p, n) < neighbor):
                flagged.setdefault(i, []).append("OUTLIER")
    return flagged


# --------------------------------------------------------------------------
# Per-file driver
# --------------------------------------------------------------------------
def process(path, args):
    info = probe(path)
    fps = info["fps"]
    nominal = int(round(float(fps)))
    start_frames, drop = (0, False)
    if info["start_tc"]:
        start_frames, drop = tc_to_frames(info["start_tc"], nominal)

    if args.temp_stats:
        tmpdir = Path(tempfile.mkdtemp(prefix="find_offline_"))
        stats_dir = tmpdir
    else:
        tmpdir = None
        stats_dir = Path(args.stats_dir) if args.stats_dir else path.parent / "qc_stats"
        stats_dir.mkdir(parents=True, exist_ok=True)
    stats_path = stats_dir / f"{path.name}.signalstats.txt"
    report_path = stats_dir / f"{path.name}.report.txt"

    lines = []

    def out(s=""):
        lines.append(s)
        print(s)

    out(f"=== {path.name}")
    out(f"  codec {info['codec']}"
        + (f" ({info['profile']})" if info["profile"] else "")
        + f", {info['pix_fmt']}, {info['width']}x{info['height']}, "
        f"{float(fps):.3f} fps, {info['bits']}-bit"
        + (f", start TC {info['start_tc']}" if info["start_tc"] else ""))
    if info["vf_format"]:
        out(f"  note: converting to {info['vf_format']} for analysis")

    try:
        if (args.reuse and stats_path.exists()
                and stats_path.stat().st_mtime >= path.stat().st_mtime):
            out(f"  reusing {stats_path}")
        else:
            run_signalstats(path, info, stats_path)
        frames = parse_stats(stats_path)
        if not frames:
            raise RuntimeError("no frames parsed from signalstats output")
        flagged = detect(frames, info["bits"], args.outlier, args.neighbor)

        out(f"  analyzed {len(frames)} frames")
        if not flagged:
            out("  RESULT: no suspect frames found")
        else:
            out(f"  RESULT: {len(flagged)} suspect frame(s)")
            for i in sorted(flagged):
                fr = frames[i]
                tc = frames_to_tc(fr["frame"] + start_frames, nominal, drop)
                out(f"    frame {fr['frame']:>8}  TC {tc}  "
                    f"[{', '.join(flagged[i])}]  "
                    f"Y={fr.get('YAVG', 0):.0f} U={fr.get('UAVG', 0):.0f} "
                    f"V={fr.get('VAVG', 0):.0f}")
        if not args.temp_stats:
            report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            out(f"  stats:  {stats_path}")
            out(f"  report: {report_path}")
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return len(flagged)


def collect_inputs(args):
    files = [Path(p) for p in args.inputs]
    if args.input_directory:
        d = Path(args.input_directory)
        if not d.is_dir():
            sys.exit(f"Not a directory: {d}")
        it = d.rglob("*") if args.recursive else d.iterdir()
        files += sorted(p for p in it
                        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
                        and not p.name.startswith("."))
    missing = [f for f in files if not f.is_file()]
    if missing:
        sys.exit("File not found: " + ", ".join(map(str, missing)))
    return files


def main():
    ap = argparse.ArgumentParser(
        description="Detect Media Offline frames / single-frame glitches.")
    ap.add_argument("inputs", nargs="*", help="video file(s) to analyze")
    ap.add_argument("-d", "--input-directory",
                    help="analyze every video file in this directory")
    ap.add_argument("-r", "--recursive", action="store_true",
                    help="with --input-directory, include subdirectories")
    ap.add_argument("--stats-dir",
                    help="where to keep stats/report files "
                         "(default: qc_stats next to each video)")
    ap.add_argument("--temp-stats", action="store_true",
                    help="use a temp directory and delete stats afterward")
    ap.add_argument("--reuse", action="store_true",
                    help="reuse an existing, up-to-date stats file")
    ap.add_argument("--outlier", type=float, default=40.0,
                    help="min YUV distance (8-bit scale) from both neighbors")
    ap.add_argument("--neighbor", type=float, default=15.0,
                    help="max YUV distance between prev and next frames")
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found on PATH")

    files = collect_inputs(args)
    if not files:
        ap.error("no input files (give files and/or --input-directory)")

    results = []  # (name, suspects or None on error)
    for f in files:
        try:
            results.append((f.name, process(f, args)))
        except Exception as e:  # keep going in batch mode
            print(f"  ERROR: {e}")
            results.append((f.name, None))
        print()

    if len(files) > 1:
        print("=== SUMMARY")
        for name, n in results:
            status = "ERROR" if n is None else ("clean" if n == 0 else f"{n} suspect frame(s)")
            print(f"  {name}: {status}")

    if any(n is None for _, n in results):
        sys.exit(2)
    sys.exit(1 if any(n for _, n in results) else 0)


if __name__ == "__main__":
    main()
