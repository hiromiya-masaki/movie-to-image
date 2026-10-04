---
name: movie-to-image
description: Split a video file (mp4, mov, webm, etc.) into image frames so you can look at it. Use when the user gives a video path and asks you to check, watch, review or analyze it, or wants frame-by-frame (コマ送り) inspection of motion such as game animations.
compatibility: Requires ffmpeg and ffprobe on PATH and Python 3.9+ (standard library only). Run with python3, or `uv run` if available.
---

# movie-to-image

You cannot read video files directly. This skill extracts frames as JPEG images, which you then view with your image-reading tool (Read in Claude Code).

## Workflow

1. **Overview first.** Run `extract` with defaults to get up to 20 frames spread evenly across the video:

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/frames.py extract /path/to/video.mp4
   ```

2. **Read the frames.** stdout is a JSON manifest. Read each `frames[].file` in order. Each file name carries its timestamp (`frame_0004_t1.30s.jpg`); `frames[].time` and `source_frame` give the same in the manifest. Cite timestamps when you answer.

3. **Zoom in on what matters.** For motion, animation timing or anything that happens between overview frames, re-extract only that range at a higher rate:

   ```
   python3 ${CLAUDE_SKILL_DIR}/scripts/frames.py extract video.mp4 --start 1.0 --end 3.0 --fps 10
   ```

   Use the video's own frame rate (see `probe`) for true frame-by-frame inspection. If the range would exceed `--max-frames`, frames are thinned and `notes` in the manifest says so; narrow the range or raise `--max-frames` rather than ignoring it.

## Commands

- `probe <video>`: duration, fps, size, total frames, audio presence.
- `extract <video>`: options below. Add `--dry-run` to see the planned timestamps without writing files.

| Option | Default | Notes |
|---|---|---|
| `--start` / `--end` | whole video | seconds |
| `--fps` | auto (≤2 fps, even spread) | explicit value gives fixed spacing |
| `--max-frames` | 20 | excess is thinned uniformly; see below |
| `--width` | 1024 | long edge in px, never upscaled. Use 1568 when small text or fine detail matters |
| `--out` | new temp dir | frames and `manifest.json` are written here |
| `--sheet COLSxROWS` | off | also writes contact sheets (row-major order, no labels; see manifest `sheets[].frames`) |

## Guidelines

- Keep batches to about 20 images. Claude.ai accepts 20 images per message, and above 20 per-image size limits tighten. The API caps at 100 per request.
- Prefer many small `--start/--end` ranges over one large extraction.
- Contact sheets cost fewer tokens but shrink each cell (a 3x3 sheet leaves ~520px per frame). Use them for a quick overview, never for judging fine motion or small details.
- This skill handles images only. Audio is not transcribed.
- Errors print to stderr with a non-zero exit code: 2 usage, 3 ffmpeg missing, 4 bad input file, 5 ffmpeg failure. If ffmpeg is missing, tell the user to install it (`brew install ffmpeg`).
