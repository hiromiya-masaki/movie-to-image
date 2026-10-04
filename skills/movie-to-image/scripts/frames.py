#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Split a video into image frames so an AI agent can look at them.

Subcommands:
  probe   <video>   Print video metadata as JSON.
  extract <video>   Extract frames (JPEG) and print a JSON manifest.

Requires ffmpeg and ffprobe on PATH. Python standard library only.
stdout is always JSON; diagnostics go to stderr.
"""
import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_MAX_FRAMES = 20  # stays under claude.ai's 20 images/message and the stricter >20 size limits
DEFAULT_WIDTH = 1024  # long edge in px; ~1.5k tokens per image on Claude
AUTO_MAX_FPS = 2.0  # overview sampling rate when --fps is not given
SHEET_MAX_EDGE = 1568  # contact sheets are scaled to fit this on both edges (Claude's standard long-edge limit)
SOFT_FRAME_WARNING = 100  # Claude API accepts at most 100 images per request (200k-context models)

EXIT_USAGE = 2
EXIT_MISSING_TOOL = 3
EXIT_BAD_INPUT = 4
EXIT_FFMPEG = 5


def fail(code, message):
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)


def require_tools():
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        fail(
            EXIT_MISSING_TOOL,
            f"{' and '.join(missing)} not found on PATH. "
            "Install ffmpeg (macOS: `brew install ffmpeg`, Debian/Ubuntu: `apt install ffmpeg`).",
        )


def run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        fail(EXIT_FFMPEG, f"{cmd[0]} failed:\n{proc.stderr.strip()[-2000:]}")
    return proc.stdout


def parse_rate(rate):
    num, _, den = rate.partition("/")
    try:
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe_video(path):
    out = run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ])
    info = json.loads(out)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        fail(EXIT_BAD_INPUT, f"no video stream found in {path}")
    fmt = info.get("format", {})
    duration = float(video.get("duration") or fmt.get("duration") or 0)
    fps = parse_rate(video.get("avg_frame_rate", "0/1")) or parse_rate(video.get("r_frame_rate", "0/1"))
    width, height = int(video["width"]), int(video["height"])
    # Honor display rotation (phone videos): swap dimensions for 90/270.
    rotation = 0
    for sd in video.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(sd["rotation"])
    rotation = int(video.get("tags", {}).get("rotate", rotation) or rotation)
    if abs(rotation) % 180 == 90:
        width, height = height, width
    return {
        "path": str(Path(path).resolve()),
        "duration_sec": round(duration, 3),
        "fps": round(fps, 3),
        "width": width,
        "height": height,
        "total_frames": int(video["nb_frames"]) if str(video.get("nb_frames", "")).isdigit()
        else int(round(duration * fps)),
        "codec": video.get("codec_name"),
        "has_audio": any(s.get("codec_type") == "audio" for s in info["streams"]),
    }


def plan_timestamps(start, end, fps, max_frames):
    """Return (timestamps, effective_fps, notes). Uniform over [start, end)."""
    span = end - start
    notes = []
    if fps is None:
        n = max(1, min(max_frames, math.ceil(span * AUTO_MAX_FPS)))
        step = span / n
        # Sample the middle of each slice so the whole range is covered evenly.
        ts = [start + (i + 0.5) * step for i in range(n)]
        return ts, n / span, notes
    n = max(1, math.floor(span * fps + 1e-9))
    if n > max_frames:
        notes.append(
            f"requested {fps:g} fps over {span:.2f}s = {n} frames; "
            f"thinned uniformly to {max_frames} (raise --max-frames or narrow --start/--end to keep {fps:g} fps)"
        )
        n = max_frames
        step = span / n
        return [start + (i + 0.5) * step for i in range(n)], n / span, notes
    return [start + i / fps for i in range(n)], fps, notes


def scale_filter(width):
    # Long edge -> `width` px, never upscale, keep aspect, even dimensions.
    return (
        f"scale=w='if(gt(iw,ih),min({width},iw),-2)':"
        f"h='if(gt(iw,ih),-2,min({width},ih))'"
    )


def extract_frame(video, t, dest, width, quality):
    run([
        "ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(video),
        "-frames:v", "1", "-an", "-vf", scale_filter(width),
        "-q:v", str(quality), str(dest),
    ])
    if not dest.exists():
        fail(EXIT_FFMPEG, f"no frame produced at t={t:.3f}s (past end of video?)")


def make_sheet(frames, cols, rows, out_dir, index):
    list_file = out_dir / f".sheet_{index:02d}.txt"
    list_file.write_text("".join(f"file '{f.resolve()}'\nduration 1\n" for f in frames))
    dest = out_dir / f"sheet_{index:02d}.jpg"
    # Shrink rows for a partial last sheet; fit every cell so the whole sheet stays within SHEET_MAX_EDGE.
    rows = min(rows, math.ceil(len(frames) / cols))
    cell_w, cell_h = SHEET_MAX_EDGE // cols - 4, SHEET_MAX_EDGE // rows - 4
    run([
        "ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-vf", f"scale={cell_w}:{cell_h}:force_original_aspect_ratio=decrease,"
               f"tile={cols}x{rows}:padding=2:margin=2:color=black", "-frames:v", "1",
        "-q:v", "2", str(dest),
    ])
    list_file.unlink()
    return dest


def cmd_probe(args):
    require_tools()
    print(json.dumps(probe_video(args.video), ensure_ascii=False, indent=2))


def cmd_extract(args):
    require_tools()
    video = Path(args.video)
    if not video.is_file():
        fail(EXIT_BAD_INPUT, f"file not found: {video}")
    if args.max_frames < 1 or args.width < 16 or (args.fps is not None and args.fps <= 0):
        fail(EXIT_USAGE, "--max-frames, --width and --fps must be positive")

    meta = probe_video(video)
    duration = meta["duration_sec"]
    start = args.start or 0.0
    end = min(args.end, duration) if args.end is not None else duration
    if start < 0 or start >= end:
        fail(EXIT_USAGE, f"invalid range: start={start}s end={end}s (video is {duration}s)")

    sheet = None
    if args.sheet:
        try:
            cols, rows = (int(x) for x in args.sheet.lower().split("x"))
            assert cols >= 1 and rows >= 1
        except (ValueError, AssertionError):
            fail(EXIT_USAGE, "--sheet must look like 3x3 (COLSxROWS)")
        sheet = (cols, rows)

    timestamps, eff_fps, notes = plan_timestamps(start, end, args.fps, args.max_frames)
    if len(timestamps) > SOFT_FRAME_WARNING:
        notes.append(
            f"{len(timestamps)} frames exceeds {SOFT_FRAME_WARNING}, the per-request image limit of the Claude API; "
            "read them in batches or use --sheet"
        )

    if args.dry_run:
        print(json.dumps({"dry_run": True, "video": meta, "frame_count": len(timestamps),
                          "effective_fps": round(eff_fps, 3),
                          "timestamps_sec": [round(t, 3) for t in timestamps], "notes": notes},
                         ensure_ascii=False, indent=2))
        return

    out_dir = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="movie-to-image-"))
    out_dir.mkdir(parents=True, exist_ok=True)

    frames = []
    for i, t in enumerate(timestamps, 1):
        name = f"frame_{i:04d}_t{t:.2f}s.jpg"
        extract_frame(video, t, out_dir / name, args.width, args.quality)
        frames.append({
            "index": i,
            "file": str(out_dir / name),
            "time_sec": round(t, 3),
            "time": f"{int(t // 60):02d}:{t % 60:05.2f}",
            "source_frame": round(t * meta["fps"]) if meta["fps"] else None,
        })
    print(f"extracted {len(frames)} frames -> {out_dir}", file=sys.stderr)

    sheets = []
    if sheet:
        per = sheet[0] * sheet[1]
        for s, off in enumerate(range(0, len(frames), per), 1):
            chunk = frames[off:off + per]
            path = make_sheet([Path(f["file"]) for f in chunk], sheet[0], sheet[1], out_dir, s)
            sheets.append({
                "file": str(path),
                "grid": f"{sheet[0]}x{sheet[1]}",
                "order": "row-major (left to right, then top to bottom)",
                "frames": [f["index"] for f in chunk],
            })

    manifest = {
        "video": meta,
        "output_dir": str(out_dir),
        "range_sec": [round(start, 3), round(end, 3)],
        "effective_fps": round(eff_fps, 3),
        "width_long_edge": args.width,
        "frame_count": len(frames),
        "frames": frames,
        "sheets": sheets,
        "notes": notes,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser(
        description="Split a video into image frames for AI agents. stdout is JSON.",
        epilog="Typical flow: `probe`, then `extract` for an overview, then `extract --start S --end E --fps 10` "
               "on the interesting range.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("probe", help="print video metadata (duration, fps, size) as JSON")
    sp.add_argument("video")
    sp.set_defaults(func=cmd_probe)

    se = sub.add_parser("extract", help="extract frames as JPEG and print a JSON manifest")
    se.add_argument("video")
    se.add_argument("--start", type=float, help="range start in seconds (default 0)")
    se.add_argument("--end", type=float, help="range end in seconds (default: end of video)")
    se.add_argument("--fps", type=float,
                    help=f"frames per second. Default: auto (up to {AUTO_MAX_FPS:g} fps, spread evenly, "
                         "capped by --max-frames)")
    se.add_argument("--max-frames", type=int, default=DEFAULT_MAX_FRAMES,
                    help=f"upper bound on frames; excess is thinned uniformly (default {DEFAULT_MAX_FRAMES})")
    se.add_argument("--width", type=int, default=DEFAULT_WIDTH,
                    help=f"long-edge size in px, never upscaled (default {DEFAULT_WIDTH})")
    se.add_argument("--quality", type=int, default=2, choices=range(1, 32), metavar="1-31",
                    help="ffmpeg JPEG -q:v, lower is better (default 2)")
    se.add_argument("--out", help="output directory (default: new temp directory)")
    se.add_argument("--sheet", metavar="COLSxROWS",
                    help="also write contact sheets, e.g. 3x3. Cells get smaller, so use for overview only")
    se.add_argument("--dry-run", action="store_true", help="print the plan without writing files")
    se.set_defaults(func=cmd_extract)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
