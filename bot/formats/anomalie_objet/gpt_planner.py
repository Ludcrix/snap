from __future__ import annotations

import json
import os
import random
import re
from dataclasses import dataclass
from pathlib import Path

import openai

from .banks import ANOMALIES, LIGHTING, OBJECTS, PLACES, SURFACES


_RECENT_PLANS_FILE = Path(__file__).resolve().parents[3] / "storage" / "formats" / "anomalie_objet" / "recent_plans.json"


@dataclass(frozen=True)
class AOPlan:
    object_name: str
    anomaly: str
    surface: str
    place: str
    lighting: str
    hook_title: str
    hashtags: list[str]
    snap_hook: str
    image_prompt: str
    subtitle_text: str | None


def _load_recent_plans(limit: int = 8) -> list[dict]:
    try:
        if not _RECENT_PLANS_FILE.exists():
            return []
        data = json.loads(_RECENT_PLANS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        out = [x for x in data if isinstance(x, dict)]
        return out[-limit:]
    except Exception:
        return []


def _append_recent_plan(plan: AOPlan, max_keep: int = 30) -> None:
    try:
        _RECENT_PLANS_FILE.parent.mkdir(parents=True, exist_ok=True)
        current = []
        if _RECENT_PLANS_FILE.exists():
            try:
                current = json.loads(_RECENT_PLANS_FILE.read_text(encoding="utf-8"))
            except Exception:
                current = []
        if not isinstance(current, list):
            current = []

        current.append(
            {
                "object": plan.object_name,
                "anomaly": plan.anomaly,
                "place": plan.place,
                "surface": plan.surface,
                "lighting": plan.lighting,
            }
        )
        current = [x for x in current if isinstance(x, dict)][-max_keep:]
        _RECENT_PLANS_FILE.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    except Exception:
        return


def _extract_first_json_object(text: str) -> str:
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON object found in response: {text}")

    stack: list[str] = []
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            stack.append("{")
        elif ch == "}":
            if not stack:
                raise ValueError(f"Malformed JSON braces in response: {text}")
            stack.pop()
            if not stack:
                return text[start : i + 1]

    raise ValueError(f"No complete JSON object found in response: {text}")


_TAG_RE = re.compile(r"^#[^\s#]{2,40}$")


def _normalize_hashtags(tags: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for t in tags or []:
        s = str(t).strip()
        if not s:
            continue
        if not s.startswith("#"):
            s = "#" + s
        s = s.replace(" ", "")
        key = s.lower()
        if key in seen:
            continue
        if not _TAG_RE.match(s):
            continue
        if "ai" in key or "ia" in key:
            continue
        seen.add(key)
        out.append(s)
    return out


def plan_anomalie_objet(*, include_subtitle: bool, rng: random.Random, log_fn=None) -> AOPlan:
    def _log(msg: str) -> None:
        if callable(log_fn):
            log_fn("AO", msg)

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY missing (expected in .env.anomalie_objet or environment)")

    gpt_model = str(os.getenv("ANOMALIE_OBJET_GPT_MODEL") or "gpt-4.1-mini").strip()
    temp_raw = str(os.getenv("ANOMALIE_OBJET_GPT_TEMPERATURE") or "0.9").strip()
    try:
        temperature = float(temp_raw)
    except Exception:
        temperature = 0.9

    variation_token = "AO-" + "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
    recent = _load_recent_plans(limit=8)

    system = (
        "You create ultra-realistic, banal smartphone snapshots for Snapchat. "
        "The result must never look staged or AI-generated. "
        "No people, no animals. One everyday object only, and one small visual anomaly only. "
        "Everything must look like an accidental phone photo from real life. "
        "Return STRICT JSON only."
    )

    allowed = {
        "objects": OBJECTS,
        "anomalies": ANOMALIES,
        "surfaces": SURFACES,
        "places": PLACES,
        "lighting": LIGHTING,
    }

    # IMPORTANT: The V2 validator forbids certain style words anywhere in image_prompt.
    # So we must instruct the model NOT to use them even as negatives.
    forbidden_words = [
        "cinematic",
        "film",
        "film still",
        "dramatic lighting",
        "moody",
        "hdr",
        "stylized",
        "bokeh",
        "shallow depth of field",
        "depth of field",
    ]

    user = (
        "Pick a new concept that is DIFFERENT from the recent concepts.\n"
        f"RECENT(JSON): {json.dumps(recent, ensure_ascii=False)}\n\n"
        "Choose values EXACTLY from these allowed lists.\n"
        f"ALLOWED(JSON): {json.dumps(allowed, ensure_ascii=False)}\n\n"
        "Constraints:\n"
        "- Photo is vertical 9:16, upright orientation (no rotation).\n"
        "- Real-world everyday indoor/outdoor lighting; neutral/cool phone white balance (avoid warm cozy amber).\n"
        "- Normal phone perspective: no fisheye, no extreme wide angle, no macro look.\n"
        "- Framing imperfect, slightly off-center; NOT symmetrical; NOT like a product photo.\n"
        "- Keep bottom ~15% relatively uncluttered (safe area for text).\n"
        "- No text in the image, no watermark, no logo.\n"
        "- No movie-like look, no studio look, no color grading, no HDR-like processing.\n"
        "- The anomaly must be obvious in under 1 second, but the scene stays credible.\n"
        "- Must feel boring/forgettable, not interesting.\n\n"
        "CRITICAL: In image_prompt, DO NOT use any of these words/phrases (not even as negatives):\n"
        f"- {', '.join(forbidden_words)}\n\n"
        "Output JSON with EXACT keys:\n"
        "{\n"
        "  \"object_name\": string,\n"
        "  \"anomaly\": string,\n"
        "  \"surface\": string,\n"
        "  \"place\": string,\n"
        "  \"lighting\": string,\n"
        "  \"hook_title\": string (French, short, factual, can include 0-1 emoji max),\n"
        "  \"hashtags\": array of 5 to 10 strings (no spaces, no AI/IA hashtags),\n"
        "  \"snap_hook\": string (French, ONE short neutral sentence, 0-2 emoji max, no hype),\n"
        "  \"subtitle_text\": string|null (French, 4-6 words, factual, no question mark),\n"
        "  \"image_prompt\": string (English; describes the photo; MUST include 'Vertical 9:16' and 'no people')\n"
        "}\n\n"
        f"Variation token: {variation_token}. Include it at the end of image_prompt."
    )

    openai.api_key = api_key
    _log(f"Planning with GPT model={gpt_model} temp={temperature}")

    resp = openai.ChatCompletion.create(
        model=gpt_model,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )

    content = resp.choices[0].message["content"]
    json_str = _extract_first_json_object(content)
    data = json.loads(json_str)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid planner JSON: {data}")

    def _pick_str(key: str) -> str:
        v = str(data.get(key) or "").strip()
        if not v:
            raise ValueError(f"planner missing '{key}'")
        return v

    obj = _pick_str("object_name")
    anomaly = _pick_str("anomaly")
    surface = _pick_str("surface")
    place = _pick_str("place")
    lighting = _pick_str("lighting")

    # Enforce exact allowed values.
    if obj not in OBJECTS:
        raise ValueError("planner object_name must be from allowed list")
    if anomaly not in ANOMALIES:
        raise ValueError("planner anomaly must be from allowed list")
    if surface not in SURFACES:
        raise ValueError("planner surface must be from allowed list")
    if place not in PLACES:
        raise ValueError("planner place must be from allowed list")
    if lighting not in LIGHTING:
        raise ValueError("planner lighting must be from allowed list")

    hook_title = _pick_str("hook_title")
    snap_hook = _pick_str("snap_hook")
    image_prompt = _pick_str("image_prompt")

    tags_raw = data.get("hashtags")
    if not isinstance(tags_raw, list):
        raise ValueError("planner hashtags must be a list")
    hashtags = _normalize_hashtags([str(x) for x in tags_raw])
    if not (5 <= len(hashtags) <= 10):
        raise ValueError(f"planner hashtags must be 5-10 (got {len(hashtags)})")

    subtitle_text = data.get("subtitle_text")
    if subtitle_text is None or subtitle_text == "":
        subtitle_text = None
    else:
        subtitle_text = str(subtitle_text).strip()

    if include_subtitle and subtitle_text is None:
        raise ValueError("planner subtitle_text must be provided when include_subtitle is true")
    if not include_subtitle:
        subtitle_text = None

    plan = AOPlan(
        object_name=obj,
        anomaly=anomaly,
        surface=surface,
        place=place,
        lighting=lighting,
        hook_title=hook_title,
        hashtags=hashtags,
        snap_hook=snap_hook,
        image_prompt=image_prompt,
        subtitle_text=subtitle_text,
    )

    _append_recent_plan(plan)
    return plan
