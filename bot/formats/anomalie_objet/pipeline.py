from __future__ import annotations

from dataclasses import dataclass
import random

from .env import load_anomalie_objet_dotenv
from .openai_image import generate_image_openai_v2

from .config import AnomalieObjetConfig, load_anomalie_objet_config
from .gpt_planner import plan_anomalie_objet
from .subtitle_generator import write_one_line_srt
from .validators import validate_factual_text, validate_image_prompt_strict
from .video_generator import generate_video_anomalie_objet


@dataclass
class AnomalieObjetResult:
    video_path: str
    object_name: str
    anomaly: str
    factual_text: str | None
    image_prompt: str
    snap_hook: str
    hook_title: str
    hashtags: list[str]


def generate_one_anomalie_objet(*, cfg: AnomalieObjetConfig | None = None, seed: int | None = None, log_fn=None) -> AnomalieObjetResult:
    def _log(prefix: str, msg: str) -> None:
        if callable(log_fn):
            log_fn(prefix, msg)

    # Load duplicated env file (if present) so this format can run independently of V1.
    load_anomalie_objet_dotenv()
    cfg = cfg or load_anomalie_objet_config()
    rng = random.Random(seed)

    cfg.images_dir.mkdir(parents=True, exist_ok=True)
    cfg.videos_dir.mkdir(parents=True, exist_ok=True)
    cfg.subtitles_dir.mkdir(parents=True, exist_ok=True)

    max_attempts = 6
    last_err: Exception | None = None
    plan = None
    image_path = None
    for attempt in range(1, max_attempts + 1):
        plan = plan_anomalie_objet(include_subtitle=bool(cfg.include_text), rng=rng, log_fn=log_fn)

        # Prompt-level validation (V2 rules).
        # Validate prompt before calling image API; if it fails, re-plan.
        try:
            validate_image_prompt_strict(plan.image_prompt)
        except ValueError as e:
            last_err = e
            _log("AO", f"[v2:ao] prompt rejected by validator (attempt {attempt}/{max_attempts}): {e}")
            continue

        _log("AO", f"Object: {plan.object_name}")
        _log("AO", f"Anomaly: {plan.anomaly}")
        _log("AO", f"Setting: {plan.surface} {plan.place} / {plan.lighting}")
        _log("AO", f"Title: {plan.hook_title}")
        _log("AO", f"Hashtags: {' '.join(plan.hashtags)}")
        _log("AO", f"Snap hook: {plan.snap_hook}")
        _log("AO", f"Prompt: {plan.image_prompt}")

        try:
            image_path = generate_image_openai_v2(
                image_prompt=plan.image_prompt,
                images_dir=str(cfg.images_dir),
                model=cfg.image_model,
                size=cfg.image_size,
                response_format="b64_json",
                log_fn=log_fn,
            )
            break
        except Exception as e:
            last_err = e
            msg = str(e)
            if "blocked by our content filters" in msg.lower():
                _log("AO", f"OpenAI blocked image request (attempt {attempt}/{max_attempts}); re-planning")
                continue
            raise

    if plan is None or image_path is None:
        raise RuntimeError(f"OpenAI blocked prompts after {max_attempts} attempts: {last_err}")

    factual_text = None
    subtitle_path = None
    if cfg.include_text:
        factual_text = str(plan.subtitle_text or "").strip()
        validate_factual_text(factual_text)
        subtitle_path = write_one_line_srt(text=factual_text, out_dir=cfg.subtitles_dir, duration_seconds=cfg.clip_seconds)
        _log("AO", f"Text: {factual_text}")

    video_path = generate_video_anomalie_objet(
        image_path=image_path,
        subtitle_path=subtitle_path,
        output_dir=cfg.videos_dir,
        seconds=cfg.clip_seconds,
        fps=cfg.fps,
        motion_seed=rng.randint(0, 2**31 - 1),
        log_fn=log_fn,
    )

    return AnomalieObjetResult(
        video_path=video_path,
        object_name=plan.object_name,
        anomaly=plan.anomaly,
        factual_text=factual_text,
        image_prompt=plan.image_prompt,
        snap_hook=plan.snap_hook,
        hook_title=plan.hook_title,
        hashtags=plan.hashtags,
    )
