import base64
import os
import re
import tempfile

import openai

from ..config import OPENAI_API_KEY


def _estimate_image_cost_usd(model: str, size: str) -> float | None:
    # Hardcoded estimates for visibility; may change over time.
    # Keep generation non-blocking if unknown.
    key = (model or "").lower().strip(), (size or "").lower().strip()

    # User expects a simple, visible estimate (example: $0.04).
    # Source: historical public pricing patterns; treat as estimate only.
    estimates = {
        ("dall-e-3", "1024x1024"): 0.04,
        ("dall-e-3", "1024x1792"): 0.04,
        ("dall-e-3", "1792x1024"): 0.04,
    }
    return estimates.get(key)


def _unique_image_path(images_dir: str, ext: str = ".png") -> str:
    os.makedirs(images_dir, exist_ok=True)
    for _ in range(100):
        candidate = os.path.join(images_dir, f"img_{next(tempfile._get_candidate_names())}{ext}")
        if not os.path.exists(candidate):
            return candidate
    raise RuntimeError("Could not allocate unique image filename")


def generate_image_openai(
    image_prompt: str,
    images_dir: str,
    model: str,
    size: str,
    response_format: str = "b64_json",
    log_fn=None,
) -> str:
    """Generate an image using OpenAI Images API and save it to disk.

    Logging uses the caller's log_fn(prefix, message) with prefix 'IMAGE'.
    """

    def _log(msg: str) -> None:
        if callable(log_fn):
            log_fn("IMAGE", msg)

    if not isinstance(image_prompt, str) or not image_prompt.strip():
        raise ValueError("image_prompt must be a non-empty string")

    def _make_safer_prompt(prompt: str, level: int) -> str:
        p = prompt
        # Replace a few higher-risk emotional/violent-adjacent terms.
        replacements = {
            r"\banguish\b": "sadness",
            r"\btrauma\b": "stress",
            r"\bterror\b": "fear",
            r"\bviolent\b": "intense",
            r"\bblood\b": "",
            r"\bweapon\b": "",
            r"\bweapons\b": "",
            r"\bscreaming\b": "shouting",
            r"\bcrying\b": "upset",
            r"\btears\b": "",
            r"\btear\b": "",
        }
        for pat, repl in replacements.items():
            p = re.sub(pat, repl, p, flags=re.IGNORECASE)

        safety_suffix = (
            " Safe-for-work, non-violent, non-graphic, non-sexual. Adult subject. "
            "No nudity. No weapons. Everyday emotional moment."
        )
        if safety_suffix.strip().lower() not in p.lower():
            p = (p + safety_suffix).strip()

        if level >= 2:
            # More conservative: avoid extra intensity words.
            p = re.sub(r"\bdark\b", "moody", p, flags=re.IGNORECASE)
            p = re.sub(r"\btense\b", "serious", p, flags=re.IGNORECASE)
            p = re.sub(r"\bfear\b", "concern", p, flags=re.IGNORECASE)
        return p

    openai.api_key = OPENAI_API_KEY

    _log(f"OpenAI prompt: {image_prompt}")
    _log(f"OpenAI image model: {model}")
    _log(f"OpenAI image size: {size}")

    est = _estimate_image_cost_usd(model, size)
    if est is None:
        _log(f"Model={model} Size={size} Estimated cost=unknown")
    else:
        _log(f"Model={model} Size={size} Estimated cost=${est:.2f}")

    last_err = None
    resp = None
    for attempt in range(3):
        prompt_to_use = image_prompt if attempt == 0 else _make_safer_prompt(image_prompt, level=attempt)
        if attempt > 0:
            _log(f"Retrying image generation with safer prompt (attempt {attempt + 1}/3)")
            _log(f"OpenAI prompt (safer): {prompt_to_use}")
        try:
            resp = openai.Image.create(
                prompt=prompt_to_use,
                n=1,
                size=size,
                model=model,
                response_format=response_format,
            )
            break
        except openai.error.InvalidRequestError as e:
            last_err = e
            msg = str(e)
            if "blocked" in msg.lower() and "content" in msg.lower() and "filter" in msg.lower():
                _log("OpenAI blocked image request by content filters")
                continue
            raise

    if resp is None:
        raise last_err

    data0 = (resp.get("data") or [{}])[0]

    is_url = "url" in data0 and bool(data0.get("url"))
    is_b64 = "b64_json" in data0 and bool(data0.get("b64_json"))

    if is_url and is_b64:
        # Unexpected but handle deterministically: prefer base64
        is_url = False

    _log(f"OpenAI response type: {'url' if is_url else 'base64' if is_b64 else 'unknown'}")

    out_path = _unique_image_path(images_dir, ext=".png")
    _log(f"Output image path: {out_path}")

    if is_b64:
        b64_data = data0.get("b64_json")
        _log(f"Returned image data length: {len(b64_data) if isinstance(b64_data, str) else 0}")
        if not isinstance(b64_data, str) or not b64_data:
            raise RuntimeError("OpenAI image response missing b64_json")
        raw = base64.b64decode(b64_data)
        with open(out_path, "wb") as f:
            f.write(raw)
    elif is_url:
        url = data0.get("url")
        _log(f"Returned image data length: {len(url) if isinstance(url, str) else 0}")
        if not isinstance(url, str) or not url:
            raise RuntimeError("OpenAI image response missing url")
        # Avoid adding heavy deps; use stdlib.
        import urllib.request

        with urllib.request.urlopen(url) as resp2:
            raw = resp2.read()
        with open(out_path, "wb") as f:
            f.write(raw)
    else:
        raise RuntimeError(f"Unexpected OpenAI image response payload: keys={list(data0.keys())}")

    file_size = os.path.getsize(out_path)
    _log(f"Saved image file size: {file_size} bytes")

    return out_path
