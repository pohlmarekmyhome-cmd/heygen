"""ffmpeg helpers: probe video size and burn ASS captions into a video."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise RuntimeError(f"{tool} not found on PATH; install ffmpeg (with libass).")


def video_size(path: Path) -> tuple[int, int]:
    """Return (width, height) as displayed, accounting for rotation metadata."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:stream_side_data=rotation:stream_tags=rotate",
            "-of", "json", str(path),
        ],
        check=True, capture_output=True, text=True,
    ).stdout
    stream = json.loads(out)["streams"][0]
    w, h = stream["width"], stream["height"]
    rotation = stream.get("tags", {}).get("rotate")
    for sd in stream.get("side_data_list", []):
        rotation = sd.get("rotation", rotation)
    if rotation is not None and abs(int(float(rotation))) % 180 == 90:
        w, h = h, w
    return w, h


def burn_captions(video: Path, ass_file: Path, output: Path, crf: int = 18, preset: str = "medium") -> None:
    """Hard-burn an ASS subtitle file into `video`, copying the audio track."""
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.stem + ".part" + output.suffix)
    # Run from the subtitle's directory so the filter argument needs no path escaping.
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video.resolve()),
        "-vf", f"ass={ass_file.name}",
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-movflags", "+faststart",
        str(tmp.resolve()),
    ]
    proc = subprocess.run(cmd, cwd=ass_file.parent, capture_output=True, text=True)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed for {video}:\n{proc.stderr.strip()}")
    tmp.replace(output)
