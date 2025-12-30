import os as _os
import subprocess
import tempfile
import sys
import json

from .image_generator import generate_image_openai


def _ffmpeg_escape_path_for_filter(path: str) -> str:
    # FFmpeg filter args live in a mini-language; this escapes Windows drive ':' and quotes.
    p = path.replace("\\", "/")
    p = p.replace(":", "\\:")
    p = p.replace("'", "\\'")
    return p


def _ffmpeg_escape_path_for_cli(path: str) -> str:
    # For Windows CLI, keep backslashes; ffmpeg handles them. This helper is mainly for consistency.
    return path


def _probe_image_rotation_degrees(image_path: str) -> int:
    # We disable FFmpeg auto-rotation; we then apply rotation ourselves if metadata requests it.
    # Returns 0, 90, 180, 270.
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
        deg = int(float(raw))
        deg = deg % 360
        if deg in (0, 90, 180, 270):
            return deg
        return 0
    except Exception:
        return 0


_VISUAL_ALLOWED = {
    "scene": {"single_person", "crowd", "empty_space"},
    "pose": {"still", "back_turned"},
    "framing": {"close", "medium", "wide"},
    "mood": {"dark", "neutral", "tense"},
    "motion": {"slow_zoom_in", "slow_zoom_out", "slight_pan"},
    "blur": {"none", "light"},
    "contrast": {"normal", "high"},
    "color": {"neutral", "cold", "warm", "desaturated"},
}


def _visual_fingerprint(visual: dict) -> str:
    return json.dumps(visual, sort_keys=True, separators=(",", ":"))


def _validate_visual_strict(visual: dict) -> None:
    if not isinstance(visual, dict):
        raise ValueError("visual must be a dict")
    if set(visual.keys()) != set(_VISUAL_ALLOWED.keys()):
        raise ValueError(f"visual keys mismatch. Expected {sorted(_VISUAL_ALLOWED.keys())} got {sorted(visual.keys())}")
    for key, allowed in _VISUAL_ALLOWED.items():
        value = visual.get(key)
        if value not in allowed:
            raise ValueError(f"visual.{key} invalid: {value}. Allowed: {sorted(allowed)}")


def generate_video(
    audio_path: str,
    subtitle_path: str,
    visual: dict,
    image_path: str | None = None,
    image_prompt: str | None = None,
    image_model: str = "dall-e-3",
    image_size: str = "1024x1792",
    expected_visual_fingerprint: str | None = None,
    log_fn=None,
) -> str:
    """
    Generates a Snapchat-style vertical video, fully procedural, with all visual logic strictly mapped from GPT JSON.
    Visual dict must contain: scene, pose, framing, mood, motion, blur, contrast, color.
    """
    def _log(msg):
        if callable(log_fn):
            log_fn("VIDEO", msg)

    _validate_visual_strict(visual)
    fp_at_entry = _visual_fingerprint(visual)
    if expected_visual_fingerprint is not None and fp_at_entry != expected_visual_fingerprint:
        raise ValueError(
            "Visual propagation mismatch: story visual != video visual. "
            f"expected={expected_visual_fingerprint} actual={fp_at_entry}"
        )

    _log(f"Working directory: {_os.getcwd()}")
    _log(f"Subtitle path: {subtitle_path}")
    _log(f"Subtitle exists: {_os.path.exists(subtitle_path)}")
    _log(f"Visual: {fp_at_entry}")

    base_dir = _os.path.dirname(_os.path.dirname(_os.path.dirname(__file__)))
    output_dir = _os.path.join(base_dir, "storage", "videos")
    _os.makedirs(output_dir, exist_ok=True)

    # Get audio duration
    audio_probe = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", audio_path
    ], capture_output=True, text=True, check=True)
    duration = float(audio_probe.stdout.strip())

    output_file = f"snap_{next(tempfile._get_candidate_names())}.mp4"
    output_path = _os.path.join(output_dir, output_file)

    # --- Image input (provided OR procedural placeholder) ---
    # Per requirements: the AI-generated image must be the ONLY visual source.
    # If we cannot obtain an image, we must fail (no procedural placeholders).
    if image_path is None:
        images_dir = _os.path.join(base_dir, "storage", "images")
        _os.makedirs(images_dir, exist_ok=True)
        if not isinstance(image_prompt, str) or not image_prompt.strip():
            raise ValueError("image_prompt is required when image_path is not provided")
        image_path = generate_image_openai(
            image_prompt=image_prompt,
            images_dir=images_dir,
            model=image_model,
            size=image_size,
            response_format="b64_json",
            log_fn=log_fn,
        )

    if not isinstance(image_path, str) or not image_path.strip():
        raise ValueError("image_path must be a non-empty string")
    if not _os.path.exists(image_path):
        raise FileNotFoundError(f"Image file not found: {image_path}")

    _log(f"Using image as background: {image_path}")

    rotate_deg = _probe_image_rotation_degrees(image_path)

    fp_after_image = _visual_fingerprint(visual)
    if fp_after_image != fp_at_entry:
        raise ValueError(
            "Visual object was mutated before animation. "
            f"before={fp_at_entry} after={fp_after_image}"
        )

    _log_fn = log_fn  # for inner helper closures

    # --- Animate image (Ken Burns) derived from visual.motion/framing/mood ---
    framing = visual.get("framing")
    motion = visual.get("motion")
    mood = visual.get("mood")

    # Base zoom target depends on framing
    if framing == "close":
        base_zoom = 1.12
    elif framing == "medium":
        base_zoom = 1.06
    else:
        base_zoom = 1.02

    if motion == "slow_zoom_in":
        z_expr = f"min(zoom+0.0007,{base_zoom})"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
    elif motion == "slow_zoom_out":
        z_expr = f"max(zoom-0.0007,1.0)"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
    else:
        # slight_pan
        z_expr = "1.03"
        x_expr = "iw/2-(iw/zoom/2)+on*1.5"
        y_expr = "ih/2-(ih/zoom/2)"

    # Optional soft fade-in based on mood
    fade_in = (mood in {"dark", "tense"})

    filters = []
    # Normalize orientation BEFORE animation, while disabling FFmpeg auto-rotation.
    if rotate_deg == 90:
        filters.append("transpose=1")
    elif rotate_deg == 270:
        filters.append("transpose=2")
    elif rotate_deg == 180:
        filters.append("transpose=2,transpose=2")

    # Force portrait format and safe pixel aspect ratio.
    filters.extend(
        [
            "scale=1080:1920:force_original_aspect_ratio=increase",
            "crop=1080:1920",
            "setsar=1",
            f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':d=1:s=1080x1920:fps=25",
        ]
    )
    if fade_in:
        filters.append("fade=t=in:st=0:d=0.4")

    # Subtitles overlay (ALWAYS LAST)
    sub_path = subtitle_path
    if sys.platform.startswith('win'):
        # Prefer relative to avoid drive-letter escaping when possible.
        try:
            sub_path = _os.path.relpath(subtitle_path, _os.getcwd())
        except Exception:
            sub_path = subtitle_path
    sub_escaped = _ffmpeg_escape_path_for_filter(sub_path)
    filters.append(f"subtitles=filename='{sub_escaped}'")

    filter_chain = ",".join(filters)

    if callable(log_fn):
        log_fn("VIDEO", "Image animated (zoom/pan)")
        log_fn("VIDEO", "Subtitles + audio overlaid")

    cmd = [
        "ffmpeg",
        "-y",
        "-noautorotate",
        "-loop",
        "1",
        "-i",
        _ffmpeg_escape_path_for_cli(image_path),
        "-i",
        audio_path,
        "-vf",
        filter_chain,
        "-t",
        str(duration),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-metadata:s:v:0",
        "rotate=0",
        "-c:a",
        "aac",
        "-shortest",
        "-movflags",
        "+faststart",
        output_path,
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        _log("FFmpeg error:")
        _log(e.stderr)
        raise

    fp_at_exit = _visual_fingerprint(visual)
    if fp_at_exit != fp_at_entry:
        raise ValueError(
            "Visual object was mutated during video generation. "
            f"before={fp_at_entry} after={fp_at_exit}"
        )
    _log(f"Output video: {output_path}")
    return output_path
