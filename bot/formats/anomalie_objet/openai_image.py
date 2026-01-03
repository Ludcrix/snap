from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path

import openai


def _unique_image_path(images_dir: str, ext: str = ".png") -> str:
    Path(images_dir).mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        candidate = Path(images_dir) / f"img_{next(tempfile._get_candidate_names())}{ext}"
        if not candidate.exists():
            return str(candidate)
    raise RuntimeError("Could not allocate unique image filename")


def generate_image_openai_v2(
    *,
    image_prompt: str,
    images_dir: str,
    model: str,
    size: str,
    response_format: str = "b64_json",
    log_fn=None,
) -> str:
    def _log(msg: str) -> None:
        if callable(log_fn):
            log_fn("IMAGE2", msg)

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY missing (expected in .env.anomalie_objet or environment)")

    if not isinstance(image_prompt, str) or not image_prompt.strip():
        raise ValueError("image_prompt must be a non-empty string")

    openai.api_key = api_key

    _log(f"OpenAI prompt: {image_prompt}")
    _log(f"OpenAI image model: {model}")
    _log(f"OpenAI image size: {size}")

    resp = openai.Image.create(
        prompt=image_prompt,
        n=1,
        size=size,
        model=model,
        response_format=response_format,
    )

    data0 = (resp.get("data") or [{}])[0]
    b64_data = data0.get("b64_json")
    if not isinstance(b64_data, str) or not b64_data:
        raise RuntimeError("OpenAI image response missing b64_json")

    out_path = _unique_image_path(images_dir, ext=".png")
    raw = base64.b64decode(b64_data)
    with open(out_path, "wb") as f:
        f.write(raw)

    try:
        _log(f"Saved image file size: {os.path.getsize(out_path)} bytes")
    except Exception:
        pass

    return out_path
