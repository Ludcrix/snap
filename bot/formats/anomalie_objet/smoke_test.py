from __future__ import annotations

import os
import subprocess

from bot.formats.anomalie_objet.env import load_anomalie_objet_dotenv
from bot.formats.anomalie_objet.config import load_anomalie_objet_config


def run() -> None:
    loaded = load_anomalie_objet_dotenv()
    print(f"[AO_TEST] dotenv loaded: {loaded if loaded else '(none found)'}")

    cfg = load_anomalie_objet_config()
    print(f"[AO_TEST] config: model={cfg.image_model} size={cfg.image_size} seconds={cfg.clip_seconds} fps={cfg.fps} include_text={cfg.include_text}")

    missing = [k for k in ["OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN"] if not str(os.getenv(k) or "").strip()]
    if missing:
        raise RuntimeError(f"[AO_TEST] Missing required env vars: {missing} (expected in .env.anomalie_objet or environment)")

    # Check ffmpeg availability (required for video output).
    try:
        p = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True)
        first_line = (p.stdout or "").splitlines()[0] if p.stdout else "ffmpeg OK"
        print(f"[AO_TEST] {first_line}")
    except Exception as e:
        raise RuntimeError(f"[AO_TEST] ffmpeg not available or failed to run: {e}")

    print("[AO_TEST] OK: ready to generate")


if __name__ == "__main__":
    run()
