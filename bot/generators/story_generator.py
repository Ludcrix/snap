import json
import os
import random
import tempfile
import re

import openai

from ..config import MODEL_NAME, OPENAI_API_KEY


VISUAL_ALLOWED = {
    "scene": ["single_person", "crowd", "empty_space"],
    "pose": ["still", "back_turned"],
    "framing": ["close", "medium", "wide"],
    "mood": ["dark", "neutral", "tense"],
    "motion": ["slow_zoom_in", "slow_zoom_out", "slight_pan"],
    "blur": ["none", "light"],
    "contrast": ["normal", "high"],
    "color": ["neutral", "cold", "warm", "desaturated"],
}


VOICE_ALLOWED = {
    "gender": {"male", "female"},
    "tone": {"calm", "tense", "intimate"},
    "pace": {"slow", "medium"},
    "energy": {"low", "medium"},
    "pitch": {"low", "normal"},
}


VISUAL_SIGNATURE_KEYS = [
    "location",
    "camera_angle",
    "framing",
    "lighting",
    "time",
    "posture",
]


_REQUIRED_MOBILE_PHRASES = [
    "vertical 9:16",
    "mobile-first composition",
    "subject centered and fully visible",
    "safe area at bottom for subtitles",
]

_REQUIRED_ORIENTATION_PHRASES = [
    "upright portrait orientation",
    "camera straight and level",
    "no tilt, no rotation, no dutch angle",
    "subject standing upright",
    "head aligned vertically",
]

_REQUIRED_STYLE_PHRASES = [
    "cinematic",
    "dramatic lighting",
    "shallow depth of field",
    "realistic photography",
]

_REQUIRED_NEGATIVE_PHRASES = [
    "no text",
    "no subtitles",
    "no watermark",
    "no logo",
    "no abstract shapes",
    "no silhouettes",
    "no distortion",
    "no deformed faces",
]


def _validate_image_prompt(image_prompt: str, location: str, camera_perspective: str, variation_token: str) -> None:
    if not isinstance(image_prompt, str) or not image_prompt.strip():
        raise ValueError("image_prompt must be a non-empty string")

    p = image_prompt.strip()
    p_lc = p.lower()

    # Heuristic English check: reject common accented characters.
    if any(ch in p for ch in "éèêàâçùûôîïöüëÉÈÊÀÂÇÙÛÔÎÏÖÜË"):
        raise ValueError("image_prompt must be written in English (accented characters detected)")

    missing = [s for s in _REQUIRED_MOBILE_PHRASES if s not in p_lc]
    if missing:
        raise ValueError(f"image_prompt missing required mobile phrases: {missing}")

    missing = [s for s in _REQUIRED_ORIENTATION_PHRASES if s not in p_lc]
    if missing:
        raise ValueError(f"image_prompt missing required orientation phrases: {missing}")

    missing = [s for s in _REQUIRED_STYLE_PHRASES if s not in p_lc]
    if missing:
        raise ValueError(f"image_prompt missing required style phrases: {missing}")

    missing = [s for s in _REQUIRED_NEGATIVE_PHRASES if s not in p_lc]
    if missing:
        raise ValueError(f"image_prompt missing required negative phrases: {missing}")

    # Must describe at least one realistic human with expressive face.
    human_terms = ["man", "woman", "person", "girl", "boy"]
    if not any(t in p_lc for t in human_terms):
        raise ValueError("image_prompt must mention at least one realistic human (man/woman/person)")

    face_terms = ["face", "facial expression", "expression", "eyes", "tear", "tears", "smile", "frown"]
    if not any(t in p_lc for t in face_terms):
        raise ValueError("image_prompt must include an expressive face / facial expression")

    # Variation requirements
    if variation_token.lower() not in p_lc:
        raise ValueError("image_prompt missing variation token")
    if location.lower() not in p_lc:
        raise ValueError("image_prompt missing required location variation")
    if camera_perspective.lower() not in p_lc:
        raise ValueError("image_prompt missing required camera perspective variation")


def _build_required_suffix(location: str, camera_perspective: str, lighting: str, variation_token: str) -> str:
    mobile_block = "; ".join(_REQUIRED_MOBILE_PHRASES)
    orientation_block = "; ".join(_REQUIRED_ORIENTATION_PHRASES)
    style_block = "; ".join(_REQUIRED_STYLE_PHRASES)
    negative_block = "; ".join(_REQUIRED_NEGATIVE_PHRASES)
    return (
        f" {mobile_block}."
        f" {orientation_block}."
        f" {style_block}."
        f" Negative prompt: {negative_block}."
        f" Location: {location}. Camera perspective: {camera_perspective}. Lighting: {lighting}."
        f" Variation token: {variation_token}."
    )


def _extract_first_json_object(text: str) -> str:
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON object found in response: {text}")

    stack = []
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


def _validate_visual_signature(sig: dict) -> None:
    if not isinstance(sig, dict):
        raise ValueError("visual_signature must be an object")
    if set(sig.keys()) != set(VISUAL_SIGNATURE_KEYS):
        raise ValueError(
            f"visual_signature keys mismatch. Expected {VISUAL_SIGNATURE_KEYS} got {sorted(sig.keys())}"
        )
    for k in VISUAL_SIGNATURE_KEYS:
        v = sig.get(k)
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"visual_signature.{k} must be a non-empty string")


def _signature_diff_fields(a: dict, b: dict) -> int:
    diffs = 0
    for k in VISUAL_SIGNATURE_KEYS:
        av = str(a.get(k) or "").strip().lower()
        bv = str(b.get(k) or "").strip().lower()
        if av != bv:
            diffs += 1
    return diffs


def _validate_visual_signature_unique(sig: dict, recent: list[dict] | None) -> None:
    if not recent:
        return
    last3 = [x for x in recent if isinstance(x, dict)][-3:]
    for prev in last3:
        _validate_visual_signature(prev)
        diffs = _signature_diff_fields(sig, prev)
        if diffs < 3:
            raise ValueError(
                f"visual_signature not unique enough vs recent signature (diff_fields={diffs}, need>=3)"
            )


def generate_story_with_visual(
    log_fn=None,
    themes: list[str] | None = None,
    forced_gender: str | None = None,
    recent_visual_signatures: list[dict] | None = None,
) -> dict:
    def _log(msg: str) -> None:
        if callable(log_fn):
            log_fn("STORY", msg)

    base_dir = os.path.dirname(os.path.dirname(__file__))
    prompts_dir = os.path.join(base_dir, "prompts")
    system_path = os.path.join(prompts_dir, "system.txt")

    with open(system_path, "r", encoding="utf-8") as f:
        system_prompt = f.read()

    default_themes = ["injustice", "malaise", "trahison"]
    active_themes = themes if isinstance(themes, list) and themes else default_themes
    active_themes = [str(t).strip() for t in active_themes if str(t).strip()]
    if not active_themes:
        active_themes = default_themes
    emotion = random.choice(active_themes)

    # Force variation in every run (even with same theme)
    location = random.choice(["room", "office", "hallway", "street", "kitchen", "stairwell"])
    camera_perspective = random.choice(["eye-level", "over-the-shoulder", "slight side view"])
    lighting = random.choice(
        [
            "window light",
            "neon light",
            "dim lamp light",
            "harsh overhead light",
            "warm practical lighting",
            "cold streetlight",
            "soft diffused light",
            "dramatic rim light",
            "flickering fluorescent light",
            "moody backlight",
        ]
    )
    variation_token = next(tempfile._get_candidate_names())
    forced_gender_norm = None
    if forced_gender in {"male", "female"}:
        forced_gender_norm = forced_gender

    protagonist_constraint = ""
    image_gender_constraint = "- At least one realistic human (man or woman) with an expressive face\n"
    if forced_gender_norm == "male":
        protagonist_constraint = (
            "IMPORTANT: The main character MUST be a man (male). "
            "Use masculine forms/pronouns consistently in French. "
            "In the 'voice' object, gender MUST be 'male'.\n"
        )
        image_gender_constraint = "- At least one realistic man with an expressive face\n"
    elif forced_gender_norm == "female":
        protagonist_constraint = (
            "IMPORTANT: The main character MUST be a woman (female). "
            "Use feminine forms/pronouns consistently in French. "
            "In the 'voice' object, gender MUST be 'female'.\n"
        )
        image_gender_constraint = "- At least one realistic woman with an expressive face\n"

    recent_block = "[]"
    try:
        last3 = [x for x in (recent_visual_signatures or []) if isinstance(x, dict)][-3:]
        recent_block = json.dumps(last3, ensure_ascii=False)
    except Exception:
        recent_block = "[]"

    prompt = (
        f"Génère une histoire courte (3-4 phrases, en français) sur le thème de {emotion}. "
        "Réponds STRICTEMENT en JSON, sans aucun texte additionnel, au format EXACT suivant :\n"
        '{"story": "<histoire>", "voice_script": "<texte voix>", "voice": {"gender": "male|female", "tone": "calm|tense|intimate", "pace": "slow|medium", "energy": "low|medium", "pitch": "low|normal"}, "hook_title": "<titre hook>", "hashtags": ["#tag"], "visual_signature": {"location": "", "camera_angle": "", "framing": "", "lighting": "", "time": "", "posture": ""}, "visual": {"scene": "", "pose": "", "framing": "", "mood": "", "motion": "", "blur": "", "contrast": "", "color": ""}, "image_prompt": "<english prompt>"}\n'
        "Contraintes :\n"
        + protagonist_constraint +
        "- 'story' = histoire (string)\n"
        "- 'voice_script' = texte à lire à haute voix (string, français), style ORAL : phrases TRÈS courtes, fragments autorisés, retours à la ligne pour les pauses, ellipses (...) fréquentes, ton parlé (pas littéraire / pas formel), aucune liste, aucun emoji, aucune balise, aucune mention de 'TikTok/Snapchat', pas de guillemets.\n"
        "- 'voice' = objet style voix (OBLIGATOIRE), choisi selon le thème :\n"
        "  gender: male|female\n"
        "  tone: calm|tense|intimate\n"
        "  pace: slow|medium\n"
        "  energy: low|medium\n"
        "  pitch: low|normal\n"
        "- 'hook_title' = titre très court, punchy, curiosité, AUCUN SPOILER. (string, français)\n"
        "- 'hashtags' = liste de hashtags (array de strings), optimisés Snap Spotlight, sans espaces. Ex: ['#storytime', '#snapspotlight']\n"
        "- 'visual_signature' = objet OBLIGATOIRE qui force la diversité visuelle, EXACTEMENT ces clés : location, camera_angle, framing, lighting, time, posture. Toutes valeurs = strings non vides.\n"
        "  RÈGLES DIVERSITÉ: chaque nouveau visual_signature DOIT différer d'AU MOINS 3 champs par rapport à CHACUN des 3 derniers signatures ci-dessous.\n"
        f"  LAST_3_VISUAL_SIGNATURES(JSON): {recent_block}\n"
        "- 'visual' = objet avec uniquement ces clés et valeurs autorisées :\n"
        "scene: single_person | crowd | empty_space\n"
        "pose: still | back_turned\n"
        "framing: close | medium | wide\n"
        "mood: dark | neutral | tense\n"
        "motion: slow_zoom_in | slow_zoom_out | slight_pan\n"
        "blur: none | light\n"
        "contrast: normal | high\n"
        "color: neutral | cold | warm | desaturated\n"
        "- 'image_prompt' = UNE seule scène cinématique en ANGLAIS, cohérente avec l'histoire et 'visual'.\n"
        "  Règles image_prompt (OBLIGATOIRES) :\n"
        "  - MUST be written in English.\n"
        f"  {image_gender_constraint}"
        "  - Clear emotional storytelling situation readable in 1 glance.\n"
        "  - Cinematic realistic photography.\n"
        "  - MUST be visually distinct from previous images: do NOT reuse the same face, the same composition, or the same environment.\n"
        "    Use the visual_signature fields to force different: location/camera_angle/framing/lighting/time/posture.\n"
        "  - ALWAYS include (exact keywords): vertical 9:16; mobile-first composition; subject centered and fully visible; safe area at bottom for subtitles.\n"
        "  - ORIENTATION LOCK (ALWAYS include): upright portrait orientation; camera straight and level; no tilt, no rotation, no dutch angle; subject standing upright; head aligned vertically.\n"
        "  - STYLE (ALWAYS include): cinematic; dramatic lighting; shallow depth of field; realistic photography.\n"
        "  - NEGATIVE PROMPT (ALWAYS include): no text; no subtitles; no watermark; no logo; no abstract shapes; no silhouettes; no distortion; no deformed faces.\n"
        "  - VARIATION (MANDATORY): random variation token + variable location (room/office/hallway/street/kitchen/stairwell) + variable camera perspective (eye-level/over-the-shoulder/slight side view).\n"
        f"    Use this variation: Location: {location}. Camera perspective: {camera_perspective}. Lighting: {lighting}. Variation token: {variation_token}.\n"
        "Aucune explication, aucune clé supplémentaire."
    )

    openai.api_key = OPENAI_API_KEY
    _log(f"Requesting story+visual (theme={emotion})")

    response = openai.ChatCompletion.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
    )
    content = response.choices[0].message["content"]

    try:
        json_str = _extract_first_json_object(content)
        data = json.loads(json_str)
    except Exception as e:
        raise ValueError(f"Invalid JSON from OpenAI: {e}\nContent: {content}")

    if (
        not isinstance(data, dict)
        or "story" not in data
        or "voice_script" not in data
        or "voice" not in data
        or "hook_title" not in data
        or "hashtags" not in data
        or "visual_signature" not in data
        or "visual" not in data
        or "image_prompt" not in data
    ):
        raise ValueError(f"JSON missing required keys: {data}")
    if not isinstance(data["story"], str):
        raise ValueError("'story' must be a string")
    if not isinstance(data["voice_script"], str) or not data["voice_script"].strip():
        raise ValueError("'voice_script' must be a non-empty string")
    if not isinstance(data["voice"], dict):
        raise ValueError("'voice' must be an object")
    if not isinstance(data["hook_title"], str) or not data["hook_title"].strip():
        raise ValueError("'hook_title' must be a non-empty string")
    if not isinstance(data["hashtags"], list) or not data["hashtags"]:
        raise ValueError("'hashtags' must be a non-empty array")
    for t in data["hashtags"]:
        if not isinstance(t, str) or not t.strip() or " " in t.strip():
            raise ValueError("Each hashtag must be a non-empty string with no spaces")
        if not t.strip().startswith("#"):
            raise ValueError("Each hashtag must start with '#'")
    if not isinstance(data["visual"], dict):
        raise ValueError("'visual' must be an object")
    _validate_visual_signature(data["visual_signature"])
    if not isinstance(data["image_prompt"], str) or not data["image_prompt"].strip():
        raise ValueError("'image_prompt' must be a non-empty string")

    if set(data.keys()) != {
        "story",
        "voice_script",
        "voice",
        "hook_title",
        "hashtags",
        "visual_signature",
        "visual",
        "image_prompt",
    }:
        raise ValueError(
            "Top-level keys mismatch. Expected ['story','voice_script','voice','hook_title','hashtags','visual_signature','visual','image_prompt'] got "
            f"{sorted(data.keys())}"
        )

    voice = data["voice"]
    if set(voice.keys()) != set(VOICE_ALLOWED.keys()):
        raise ValueError(
            f"Voice keys mismatch. Expected {sorted(VOICE_ALLOWED.keys())} got {sorted(voice.keys())}"
        )
    for key, allowed in VOICE_ALLOWED.items():
        v = voice.get(key)
        if v not in allowed:
            raise ValueError(f"Invalid value for voice.{key}: {v}. Allowed: {sorted(allowed)}")

    if forced_gender_norm and voice.get("gender") != forced_gender_norm:
        raise ValueError(
            f"voice.gender mismatch. Forced={forced_gender_norm} got {voice.get('gender')}"
        )

    _validate_visual_signature_unique(data["visual_signature"], recent_visual_signatures)

    visual = data["visual"]
    required_keys = set(VISUAL_ALLOWED.keys())
    if set(visual.keys()) != required_keys:
        raise ValueError(f"Visual keys mismatch. Expected {sorted(required_keys)} got {sorted(visual.keys())}")

    for key, allowed_values in VISUAL_ALLOWED.items():
        value = visual.get(key)
        if value not in allowed_values:
            raise ValueError(f"Invalid value for '{key}': {value}. Allowed: {allowed_values}")

    def _call_gpt(user_prompt: str) -> dict:
        response = openai.ChatCompletion.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        content = response.choices[0].message["content"]
        json_str = _extract_first_json_object(content)
        return json.loads(json_str)

    # If rules are not met, regenerate image_prompt (keeping story+visual fixed).
    story_text = data["story"]
    voice_script = data["voice_script"].strip()
    voice_style = voice
    hook_title = data["hook_title"].strip()
    hashtags = [str(x).strip() for x in data["hashtags"]]
    visual_signature = data["visual_signature"]
    visual_fixed = visual
    image_prompt = data["image_prompt"].strip()

    for attempt in range(3):
        suffix = _build_required_suffix(location, camera_perspective, lighting, variation_token)
        if suffix.strip() not in image_prompt:
            image_prompt = (image_prompt + " " + suffix).strip()

        try:
            _validate_image_prompt(image_prompt, location, camera_perspective, variation_token)

            # Extra consistency check when forced gender is selected via Telegram.
            p_lc = image_prompt.lower()
            if forced_gender_norm == "male" and not re.search(r"\bman\b", p_lc):
                raise ValueError("image_prompt must explicitly mention a man when forced_gender=male")
            if forced_gender_norm == "female" and not re.search(r"\bwoman\b", p_lc):
                raise ValueError("image_prompt must explicitly mention a woman when forced_gender=female")
            break
        except Exception as e:
            if attempt == 2:
                raise

            # New variation for the retry
            location = random.choice(["room", "office", "hallway", "street", "kitchen", "stairwell"])
            camera_perspective = random.choice(["eye-level", "over-the-shoulder", "slight side view"])
            lighting = random.choice(
                [
                    "window light",
                    "neon light",
                    "dim lamp light",
                    "harsh overhead light",
                    "dramatic lighting",
                    "soft diffused light",
                ]
            )
            variation_token = next(tempfile._get_candidate_names())

            _log(f"image_prompt failed validation; regenerating (reason={e})")

            repair_prompt = (
                "Rewrite ONLY the image_prompt for the provided story, voice_script, voice, and visual. "
                "Return STRICT JSON ONLY with EXACT keys: story, voice_script, voice, hook_title, hashtags, visual_signature, visual, image_prompt. "
                "The story, voice_script, voice, hook_title, hashtags, visual_signature, and visual must be IDENTICAL to the provided ones.\n"
                f"STORY: {story_text}\n"
                f"VOICE_SCRIPT: {voice_script}\n"
                f"VOICE(JSON): {json.dumps(voice_style, sort_keys=True)}\n"
                f"HOOK_TITLE: {hook_title}\n"
                f"HASHTAGS(JSON): {json.dumps(hashtags, ensure_ascii=False)}\n"
                f"VISUAL_SIGNATURE(JSON): {json.dumps(visual_signature, ensure_ascii=False)}\n"
                f"VISUAL(JSON): {json.dumps(visual_fixed, sort_keys=True)}\n"
                "IMAGE_PROMPT RULES (English):\n"
                + image_gender_constraint +
                "- Clear emotional storytelling situation\n"
                "- Cinematic, realistic photography style\n"
                "- MUST be visually distinct from previous images: do NOT reuse the same face, the same composition, or the same environment\n"
                "ALWAYS include: vertical 9:16; mobile-first composition; subject centered and fully visible; safe area at bottom for subtitles\n"
                "ORIENTATION LOCK (ALWAYS include): upright portrait orientation; camera straight and level; no tilt, no rotation, no dutch angle; subject standing upright; head aligned vertically\n"
                "STYLE (ALWAYS include): cinematic; dramatic lighting; shallow depth of field; realistic photography\n"
                "NEGATIVE PROMPT (ALWAYS include): no text; no subtitles; no watermark; no logo; no abstract shapes; no silhouettes; no distortion; no deformed faces\n"
                "VARIATION (MANDATORY): random variation token + location (room/office/hallway/street/kitchen/stairwell) + camera perspective (eye-level/over-the-shoulder/slight side view)\n"
                f"Use this variation: Location: {location}. Camera perspective: {camera_perspective}. Lighting: {lighting}. Variation token: {variation_token}.\n"
            )

            repaired = _call_gpt(repair_prompt)
            if repaired.get("story") != story_text:
                raise ValueError("Regenerated JSON changed story")
            if repaired.get("voice_script") != voice_script:
                raise ValueError("Regenerated JSON changed voice_script")
            if repaired.get("voice") != voice_style:
                raise ValueError("Regenerated JSON changed voice")
            if repaired.get("hook_title") != hook_title:
                raise ValueError("Regenerated JSON changed hook_title")
            if repaired.get("hashtags") != hashtags:
                raise ValueError("Regenerated JSON changed hashtags")
            if repaired.get("visual_signature") != visual_signature:
                raise ValueError("Regenerated JSON changed visual_signature")
            if repaired.get("visual") != visual_fixed:
                raise ValueError("Regenerated JSON changed visual")
            if not isinstance(repaired.get("image_prompt"), str) or not repaired["image_prompt"].strip():
                raise ValueError("Regenerated image_prompt is not a non-empty string")
            image_prompt = repaired["image_prompt"].strip()

    return {
        "story": story_text,
        "voice_script": voice_script,
        "voice": voice_style,
        "hook_title": hook_title,
        "hashtags": hashtags,
        "visual_signature": visual_signature,
        "visual": visual_fixed,
        "image_prompt": image_prompt,
    }


def generate_story(
    log_fn=None,
    themes: list[str] | None = None,
    forced_gender: str | None = None,
    recent_visual_signatures: list[dict] | None = None,
) -> dict:
    return generate_story_with_visual(
        log_fn=log_fn,
        themes=themes,
        forced_gender=forced_gender,
        recent_visual_signatures=recent_visual_signatures,
    )
