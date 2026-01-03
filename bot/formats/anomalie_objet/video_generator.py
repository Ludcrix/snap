from __future__ import annotations

import os
import random
import subprocess
import tempfile
import sys
from pathlib import Path


def _ffmpeg_escape_path_for_filter(path: str) -> str:
    # FFmpeg filter args live in a mini-language; this escapes Windows drive ':' and quotes.
    p = path.replace("\\", "/")
    p = p.replace(":", "\\:")
    p = p.replace("'", "\\'")
    return p


def _probe_image_rotation_degrees(image_path: str) -> int:
    try:
        p = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream_tags=rotate",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                image_path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        raw = (p.stdout or "").strip()
        if not raw:
            return 0
        deg = int(float(raw)) % 360
        return deg if deg in (0, 90, 180, 270) else 0
    except Exception:
        return 0


def generate_video_anomalie_objet(
    *,
    image_path: str,
    subtitle_path: str | None,
    output_dir: Path,
    seconds: float,
    fps: int,
    motion_mode: str | None = None,
    motion_seed: int | None = None,
    log_fn=None,
) -> str:
    def _log(msg: str) -> None:
        if callable(log_fn):
            log_fn("VIDEO2", msg)

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"ao_{next(tempfile._get_candidate_names())}.mp4"

    if not isinstance(image_path, str) or not image_path.strip() or not os.path.exists(image_path):
        raise FileNotFoundError(f"Image file not found: {image_path}")

    rotate_deg = _probe_image_rotation_degrees(image_path)

    # Banal, crédible, mobile-first: animation quasi imperceptible.
    # Choose exactly ONE option per video.
    rng = random.Random(motion_seed)
    modes = ["micro_zoom", "handheld_jitter", "light_drift", "light_grain"]
    # Bias toward subtle geometric motion so the output doesn't feel "frozen".
    mode = (motion_mode or "").strip().lower() or rng.choices(
        modes,
        weights=[0.55, 0.30, 0.10, 0.05],
        k=1,
    )[0]
    if mode not in set(modes):
        mode = rng.choices(modes, weights=[0.55, 0.30, 0.10, 0.05], k=1)[0]

    seconds = float(seconds)
    if seconds <= 0:
        seconds = 5.0
    fps_i = int(fps) if int(fps) > 0 else 24
    total_frames = max(1, int(round(seconds * fps_i)))

    filters: list[str] = []
    if rotate_deg == 90:
        filters.append("transpose=1")
    elif rotate_deg == 270:
        filters.append("transpose=2")
    elif rotate_deg == 180:
        filters.append("transpose=2,transpose=2")

    # Base: scale/crop to vertical 9:16 (upright) and set SAR.
    filters.extend(
        [
            "scale=1080:1920:force_original_aspect_ratio=increase",
            "crop=1080:1920",
            "setsar=1",
        ]
    )

    if mode == "micro_zoom":
        # Linear micro zoom (no easing), max 1.03.
        zoom_max = min(1.03, rng.uniform(1.02, 1.03))
        dz = max(0.0, float(zoom_max) - 1.0)
        denom = max(1, total_frames - 1)
        z_expr = f"1+{dz:.6f}*on/{denom}"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
        filters.append(f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':d=1:s=1080x1920:fps={fps_i}")
    elif mode == "light_drift":
        # Tiny brightness drift (<= ~3%) and slow.
        amp = rng.uniform(0.015, 0.030)
        period = max(6.0, seconds * 3.0)
        filters.append(f"eq=brightness='{amp:.4f}*sin(2*PI*t/{period:.3f})'")
    elif mode == "light_grain":
        # Very light temporal grain to avoid "too clean" look.
        strength = rng.choice([3, 4])
        filters.append(f"noise=alls={strength}:allf=t")
    elif mode == "handheld_jitter":
        # Tiny handheld-like tremble (±1..±2 px), no visible pan.
        # Implemented via zoompan to stay compatible across ffmpeg builds.
        amp = float(rng.choice([1, 2]))
        # Small constant zoom to hide border movement.
        z = float(rng.uniform(1.010, 1.020))

        # Two slow-ish sine components with randomized periods and phases.
        p1 = float(rng.uniform(35.0, 70.0))
        p2 = float(rng.uniform(18.0, 40.0))
        ph1 = float(rng.uniform(0.0, 6.283185))
        ph2 = float(rng.uniform(0.0, 6.283185))
        ax2 = amp * 0.7
        ay2 = amp * 0.6
        dx = f"{amp:.3f}*sin(2*PI*on/{p1:.3f}+{ph1:.6f})+{ax2:.3f}*sin(2*PI*on/{p2:.3f}+{ph2:.6f})"
        dy = f"{amp:.3f}*sin(2*PI*on/{p2:.3f}+{ph2:.6f})+{ay2:.3f}*sin(2*PI*on/{p1:.3f}+{ph1:.6f})"

        z_expr = f"{z:.6f}"
        x_expr = f"iw/2-(iw/zoom/2)+({dx})"
        y_expr = f"ih/2-(ih/zoom/2)+({dy})"
        filters.append(f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':d=1:s=1080x1920:fps={fps_i}")

    if subtitle_path:
        sub_path = subtitle_path
        if sys.platform.startswith("win"):
            try:
                sub_path = os.path.relpath(subtitle_path, os.getcwd())
            except Exception:
                sub_path = subtitle_path
        sub_escaped = _ffmpeg_escape_path_for_filter(sub_path)
        filters.append(f"subtitles=filename='{sub_escaped}'")

    filter_chain = ",".join(filters)

    _log(f"Motion mode: {mode}")

    cmd = [
        "ffmpeg",
        "-y",
        "-noautorotate",
        "-loop",
        "1",
        "-i",
        image_path,
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-t",
        f"{seconds:.2f}",
        "-vf",
        filter_chain,
        "-r",
        str(int(fps_i)),
        "-pix_fmt",
        "yuv420p",
        "-shortest",
        str(out_path),
    ]

    _log(f"FFmpeg output: {out_path}")
    _log("Running ffmpeg (subtle motion)")
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {p.stderr}\nCMD: {' '.join(cmd)}")

    return str(out_path)
