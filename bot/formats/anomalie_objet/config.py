import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AnomalieObjetConfig:
    image_model: str = "dall-e-3"
    image_size: str = "1024x1792"  # 9:16
    clip_seconds: float = 5.5
    fps: int = 25
    include_text: bool = True

    storage_root: Path = Path(__file__).resolve().parents[3] / "storage" / "formats" / "anomalie_objet"

    @property
    def images_dir(self) -> Path:
        return self.storage_root / "images"

    @property
    def videos_dir(self) -> Path:
        return self.storage_root / "videos"

    @property
    def subtitles_dir(self) -> Path:
        return self.storage_root / "subtitles"


def load_anomalie_objet_config() -> AnomalieObjetConfig:
    # Dedicated env keys to keep this format configurable without touching V1.
    model = str(os.getenv("ANOMALIE_OBJET_IMAGE_MODEL") or "dall-e-3").strip()
    size = str(os.getenv("ANOMALIE_OBJET_IMAGE_SIZE") or "1024x1792").strip()

    include_text_raw = str(os.getenv("ANOMALIE_OBJET_INCLUDE_TEXT") or "1").strip().lower()
    include_text = include_text_raw not in {"0", "false", "no", "off"}

    clip_seconds_raw = str(os.getenv("ANOMALIE_OBJET_CLIP_SECONDS") or "5.5").strip()
    try:
        clip_seconds = float(clip_seconds_raw)
    except Exception:
        clip_seconds = 5.5

    fps_raw = str(os.getenv("ANOMALIE_OBJET_FPS") or "25").strip()
    try:
        fps = int(fps_raw)
    except Exception:
        fps = 25

    return AnomalieObjetConfig(
        image_model=model,
        image_size=size,
        clip_seconds=clip_seconds,
        fps=fps,
        include_text=include_text,
    )
