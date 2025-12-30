import json
from dataclasses import dataclass
from collections import deque
from pathlib import Path
import threading

from bot.generators.story_generator import generate_story
from bot.generators.subtitle_generator import generate_subtitles
from bot.generators.voice_generator import VOICE_LEAD_IN_SILENCE_SECONDS, generate_voice_with_duration
from bot.generators.video_generator import generate_video


_RECENT_VISUAL_SIGNATURES: deque[dict] = deque(maxlen=3)
_RECENT_SIG_LOCK = threading.Lock()
_RECENT_SIG_FILE = Path(__file__).resolve().parent.parent / "storage" / "recent_visual_signatures.json"


def _load_recent_visual_signatures() -> None:
    try:
        if not _RECENT_SIG_FILE.exists():
            return
        raw = _RECENT_SIG_FILE.read_text(encoding="utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, list):
            return
        cleaned = [x for x in payload if isinstance(x, dict)][-3:]
        _RECENT_VISUAL_SIGNATURES.clear()
        _RECENT_VISUAL_SIGNATURES.extend(cleaned)
    except Exception:
        return


def _save_recent_visual_signatures() -> None:
    try:
        _RECENT_SIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = list(_RECENT_VISUAL_SIGNATURES)[-3:]
        tmp = _RECENT_SIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_RECENT_SIG_FILE)
    except Exception:
        return


def _validate_visual_signature_unique(sig: dict, recent: list[dict]) -> None:
    keys = ["location", "camera_angle", "framing", "lighting", "time", "posture"]
    for prev in recent[-3:]:
        diffs = 0
        for k in keys:
            if str(sig.get(k) or "").strip().lower() != str(prev.get(k) or "").strip().lower():
                diffs += 1
        if diffs < 3:
            raise ValueError(f"visual_signature not unique enough (diff_fields={diffs}, need>=3)")


# Load persisted signatures once per process start.
_load_recent_visual_signatures()


@dataclass
class ClipResult:
    video_path: str
    hook_title: str
    hashtags: list[str]


def generate_one_clip(
    *,
    themes: list[str] | None,
    voice_mode: str,  # 'auto' | 'male' | 'female'
    log_fn=None,
) -> ClipResult:
    forced_gender = voice_mode if voice_mode in {"male", "female"} else None
    with _RECENT_SIG_LOCK:
        recent = list(_RECENT_VISUAL_SIGNATURES)

    story_obj = generate_story(
        log_fn=log_fn,
        themes=themes,
        forced_gender=forced_gender,
        recent_visual_signatures=recent,
    )

    # Orchestration-only safety override (should already be enforced by GPT prompt).
    if voice_mode in {"male", "female"} and isinstance(story_obj.get("voice"), dict):
        story_obj["voice"]["gender"] = voice_mode

    visual_fingerprint = json.dumps(story_obj["visual"], sort_keys=True, separators=(",", ":"))

    voice_script = str(story_obj["voice_script"]).strip()
    voice_style = story_obj.get("voice")
    audio_path, audio_duration = generate_voice_with_duration(voice_script, voice_style=voice_style, log_fn=log_fn)

    subtitle_path = generate_subtitles(
        story_obj["story"],
        audio_duration_seconds=audio_duration,
        start_offset_seconds=VOICE_LEAD_IN_SILENCE_SECONDS,
    )

    visual_signature = story_obj.get("visual_signature")
    if isinstance(visual_signature, dict):
        if callable(log_fn):
            log_fn("IMAGE", f"Visual signature: {json.dumps(visual_signature, ensure_ascii=False)}")
        with _RECENT_SIG_LOCK:
            _validate_visual_signature_unique(visual_signature, list(_RECENT_VISUAL_SIGNATURES))
        if callable(log_fn):
            log_fn("IMAGE", "Visual signature validated (unique)")

    # Strengthen diversity instruction for DALL·E (prompt-only; video pipeline unchanged).
    image_prompt = str(story_obj["image_prompt"]).strip()
    image_prompt += (
        "\n\nDIVERSITY REQUIREMENT: This image MUST be visually distinct from the previous images. "
        "Do NOT reuse the same face, the same composition, or the same environment. "
        "Change location/camera_angle/framing/lighting/time/posture."
    )

    video_path = generate_video(
        audio_path,
        subtitle_path,
        story_obj["visual"],
        image_path=None,
        image_prompt=image_prompt,
        expected_visual_fingerprint=visual_fingerprint,
        log_fn=log_fn,
    )

    if isinstance(visual_signature, dict):
        with _RECENT_SIG_LOCK:
            _RECENT_VISUAL_SIGNATURES.append(visual_signature)
            _save_recent_visual_signatures()

    return ClipResult(
        video_path=video_path,
        hook_title=str(story_obj.get("hook_title") or "").strip(),
        hashtags=[str(x).strip() for x in (story_obj.get("hashtags") or []) if str(x).strip()],
    )
