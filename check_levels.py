#!/usr/bin/env python3
"""
check_levels.py - ffmpeg wrapper that measures integrated loudness (LUFS),
loudness range (LU) and true peak (dBTP) of audio/video files, and
optionally checks them against a delivery standard or custom targets.

Uses ffmpeg's ebur128 filter (ITU-R BS.1770 / EBU R128 measurement).

Exit codes (bitmask, so you can tell what failed):
    0   all files pass (or no check requested)
    1   loudness out of range on at least one file
    2   true peak over limit on at least one file
    3   both of the above
    10  error (ffmpeg missing, unreadable file, no audio, parse failure)
    (10 wins if any file errored)

Examples:
    check_levels.py mix.wav
    check_levels.py film.mov --standard netflix
    check_levels.py ep1.mp4 ep2.mp4 --standard ebu-r128 --json
    check_levels.py film51.mov --standard netflix --channel C   # center-channel loudness
    check_levels.py spot.wav --target -24 --tolerance 2 --tp-limit -2
    check_levels.py --list-standards
"""

import argparse
import json
import math
import re
import shutil
import subprocess
import sys

# --------------------------------------------------------------------------
# Delivery presets: (target LUFS, +/- tolerance in LU, true-peak ceiling dBTP, note)
#
# ffmpeg itself has no named-standard presets (its loudnorm filter defaults to
# I=-24, TP=-2, and ebur128 only knows EBU R128's -23 reference), so these are
# a hand-maintained table. Delivery specs change - VERIFY against the
# distributor's current technical spec before a real delivery.
#
# "dialog-gated" specs (Netflix, Disney+, etc.) measure loudness only over
# speech. ffmpeg cannot do dialogue gating, so a whole-program measurement is
# an approximation for those.
# --------------------------------------------------------------------------
STANDARDS = {
    # Broadcast
    "ebu-r128":   (-23.0, 0.5, -1.0, "EBU R128 (Europe broadcast), program loudness"),
    "atsc-a85":   (-24.0, 2.0, -2.0, "ATSC A/85 (US broadcast, CALM Act), LKFS"),
    "arib-tr-b32": (-24.0, 1.0, -1.0, "ARIB TR-B32 (Japan broadcast)"),
    "oztam":      (-23.0, 1.0, -1.0, "OP-59 (Australia broadcast, same -23 as R128)"),
    # Streaming video (check current specs!)
    "netflix":    (-27.0, 2.0, -2.0, "Netflix 5.1/stereo, DIALOG-GATED (ffmpeg approximates)"),
    "amazon":     (-24.0, 2.0, -2.0, "Amazon Prime Video, verify gating/tolerance"),
    "appletv":    (-24.0, 2.0, -2.0, "Apple TV+, verify gating/tolerance"),
    "disney":     (-27.0, 2.0, -2.0, "Disney+, DIALOG-GATED (ffmpeg approximates)"),
    # Music / audio streaming (normalization targets, not hard deliverable specs)
    "spotify":    (-14.0, 1.0, -1.0, "Spotify normalization target"),
    "youtube":    (-14.0, 1.0, -1.0, "YouTube normalization reference"),
    "apple-music": (-16.0, 1.0, -1.0, "Apple Music / Sound Check"),
    "podcast":    (-16.0, 1.0, -1.0, "Common podcast target (Apple/Spotify guidance)"),
}

ALIASES = {
    "r128": "ebu-r128", "ebu": "ebu-r128",
    "atsc": "atsc-a85", "a85": "atsc-a85", "a/85": "atsc-a85",
    "arib": "arib-tr-b32",
    "prime": "amazon", "primevideo": "amazon", "amazon-prime": "amazon",
    "apple-tv": "appletv", "apple-tv+": "appletv", "appletv+": "appletv",
    "disney+": "disney", "applemusic": "apple-music",
}

EXIT_LOUDNESS = 1
EXIT_PEAK = 2
EXIT_ERROR = 10


def list_standards():
    print(f"{'name':<13}{'target':>8}{'tol':>8}{'TP max':>9}   notes")
    print("-" * 78)
    for name, (tgt, tol, tp, note) in STANDARDS.items():
        print(f"{name:<13}{tgt:>8.1f}{'+-' + format(tol, 'g'):>8}{tp:>9.1f}   {note}")
    print("\nSpecs change - verify against the distributor's current deliverables doc.")


CHANNEL_ALIASES = {"C": "FC", "CENTER": "FC", "CENTRE": "FC", "L": "FL", "R": "FR",
                   "LS": "SL", "RS": "SR", "SUB": "LFE"}


def channel_expr(ch):
    """Turn a user-supplied channel into an ffmpeg pan source: FC, FL, ... or c<N>."""
    c = ch.strip()
    if c.isdigit():                      # 0-based index, e.g. 2 -> c2
        return f"c{c}"
    if re.fullmatch(r"[cC]\d+", c):      # explicit c2 style
        return c.lower()
    c = c.upper()
    return CHANNEL_ALIASES.get(c, c)


def measure(path, stream=None, channel=None):
    """Run ffmpeg ebur128 and return dict(i, lra, tp). Raises RuntimeError.

    If channel is given, only that channel is measured (pan=mono|c0=<ch>).
    """
    flt = "ebur128=peak=true:framelog=quiet"
    if channel:
        flt = f"pan=mono|c0={channel_expr(channel)}," + flt
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-nostdin",
        "-i", path,
        "-map", f"0:a:{stream}" if stream is not None else "0:a:0",
        "-vn",
        "-filter:a", flt,
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    err = proc.stderr

    if proc.returncode != 0:
        if "Stream map" in err or "matches no streams" in err:
            raise RuntimeError("no audio stream found")
        last = [ln for ln in err.strip().splitlines() if ln.strip()][-1:] or ["unknown ffmpeg error"]
        raise RuntimeError(last[0])

    # Only look at the final summary block
    idx = err.rfind("Summary:")
    if idx == -1:
        raise RuntimeError("could not find ebur128 summary in ffmpeg output")
    summary = err[idx:]

    def grab(pattern):
        m = re.search(pattern, summary)
        if not m:
            raise RuntimeError(f"could not parse '{pattern}' from ffmpeg output")
        v = m.group(1)
        return -math.inf if v == "-inf" else float(v)

    return {
        "integrated_lufs": grab(r"\bI:\s+(-?[\d.]+|-inf)\s+LUFS"),
        "lra_lu": grab(r"\bLRA:\s+(-?[\d.]+|-inf)\s+LU"),
        "true_peak_dbtp": grab(r"\bPeak:\s+(-?[\d.]+|-inf)\s+dBFS"),
    }


def fmt(v):
    return "-inf" if v == -math.inf else f"{v:.1f}"


def main():
    ap = argparse.ArgumentParser(
        description="Measure loudness / true peak with ffmpeg and optionally check against a standard.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run with --list-standards to see presets.",
    )
    ap.add_argument("files", nargs="*", help="audio or video file(s)")
    ap.add_argument("-s", "--standard", help="preset name (see --list-standards)")
    ap.add_argument("-t", "--target", type=float, help="target integrated loudness in LUFS (overrides preset)")
    ap.add_argument("--tolerance", type=float, help="allowed +/- LU around target (overrides preset; default 2 if only --target given)")
    ap.add_argument("--tp-limit", type=float, help="true peak ceiling in dBTP (overrides preset)")
    ap.add_argument("--stream", type=int, help="audio stream index to measure (default: first audio stream)")
    ap.add_argument("--channel", metavar="CH",
                    help="measure LOUDNESS on one channel only (e.g. C for the 5.1 center, "
                         "or a 0-based index like 2). Approximates dialogue gating. "
                         "True peak is still measured on the full mix.")
    ap.add_argument("--json", action="store_true", help="output JSON instead of text")
    ap.add_argument("--list-standards", action="store_true", help="show built-in presets and exit")
    args = ap.parse_args()

    if args.list_standards:
        list_standards()
        return 0
    if not args.files:
        ap.error("no input files given")
    if not shutil.which("ffmpeg"):
        print("error: ffmpeg not found in PATH", file=sys.stderr)
        return EXIT_ERROR

    target = tol = tp_limit = None
    note = None
    if args.standard:
        key = args.standard.lower()
        key = ALIASES.get(key, key)
        if key not in STANDARDS:
            print(f"error: unknown standard '{args.standard}'. Try --list-standards.", file=sys.stderr)
            return EXIT_ERROR
        target, tol, tp_limit, note = STANDARDS[key]
    if args.target is not None:
        target = args.target
        if args.tolerance is None and tol is None:
            tol = 2.0
    if args.tolerance is not None:
        tol = args.tolerance
    if args.tp_limit is not None:
        tp_limit = args.tp_limit
    if tol is not None and target is None:
        ap.error("--tolerance needs --target or --standard")

    checking = target is not None or tp_limit is not None
    code = 0
    results = []

    for path in args.files:
        entry = {"file": path}
        try:
            m = measure(path, args.stream)
            ch = None
            if args.channel:
                ch = measure(path, args.stream, args.channel)
                full = m
                # loudness/LRA from the channel; true peak from the full mix
                m = {"integrated_lufs": ch["integrated_lufs"],
                     "lra_lu": ch["lra_lu"],
                     "true_peak_dbtp": full["true_peak_dbtp"]}
                entry["channel"] = args.channel
                entry["full_mix_integrated_lufs"] = full["integrated_lufs"]
                entry["full_mix_lra_lu"] = full["lra_lu"]
                entry["channel_true_peak_dbtp"] = ch["true_peak_dbtp"]
        except RuntimeError as e:
            entry["error"] = str(e)
            code |= EXIT_ERROR
            results.append(entry)
            if not args.json:
                print(f"{path}\n  ERROR: {e}\n", file=sys.stderr)
            continue

        entry.update(m)
        lo_ok = tp_ok = None
        if target is not None:
            lo_ok = abs(m["integrated_lufs"] - target) <= tol if math.isfinite(m["integrated_lufs"]) else False
            entry["loudness_pass"] = lo_ok
            if not lo_ok:
                code |= EXIT_LOUDNESS
        if tp_limit is not None:
            tp_ok = m["true_peak_dbtp"] <= tp_limit
            entry["true_peak_pass"] = tp_ok
            if not tp_ok:
                code |= EXIT_PEAK
        results.append(entry)

        if not args.json:
            print(path)
            if args.channel:
                print(f"  [channel {args.channel.upper()} loudness - dialogue-gating approximation, not a Dolby/Netflix-verified number]")
            line = f"  Integrated: {fmt(m['integrated_lufs']):>6} LUFS"
            if lo_ok is not None:
                diff = m["integrated_lufs"] - target
                line += f"   (target {target:g} +/-{tol:g}; {diff:+.1f} LU)  {'PASS' if lo_ok else 'FAIL'}"
            print(line)
            print(f"  LRA:        {fmt(m['lra_lu']):>6} LU")
            line = f"  True peak:  {fmt(m['true_peak_dbtp']):>6} dBTP"
            if args.channel:
                line += " (full mix)"
            if tp_ok is not None:
                line += f"   (limit {tp_limit:g})  {'PASS' if tp_ok else 'FAIL'}"
            print(line)
            if args.channel:
                print(f"  Full mix:   {fmt(entry['full_mix_integrated_lufs']):>6} LUFS, "
                      f"LRA {fmt(entry['full_mix_lra_lu'])} LU  (for reference)")
            print()

    if args.json:
        print(json.dumps({"standard": args.standard, "target": target, "tolerance": tol,
                          "tp_limit": tp_limit, "results": results}, indent=2,
                         default=lambda o: str(o)))
    elif checking:
        if code == 0:
            print("RESULT: PASS" + (f"  [{note}]" if note else ""))
        else:
            print(f"RESULT: FAIL (exit code {code})" + (f"  [{note}]" if note else ""))

    return code


if __name__ == "__main__":
    sys.exit(main())
