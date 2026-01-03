from __future__ import annotations

import re


FORBIDDEN_STYLE_TERMS = [
    "cinematic",
    "film still",
    "dramatic lighting",
    "moody",
    "moody atmosphere",
    "shallow depth of field",
    "depth of field",
    "bokeh",
    "hdr",
    "stylized",
]

FORBIDDEN_CONTENT_PATTERNS = [
    r"\bperson\b",
    r"\bpeople\b",
    r"\bhuman\b",
    r"\bface\b",
    r"\bhands\b",
    r"\bhand\b",
    r"\bman\b",
    r"\bwoman\b",
    r"\bchild\b",
    r"\bdog\b",
    r"\bcat\b",
    r"\banimal\b",
]


def validate_factual_text(text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be non-empty")
    if "?" in text:
        raise ValueError("text must not be a question")
    words = [w for w in text.replace("'", " ").split() if w.strip()]
    if not (4 <= len(words) <= 6):
        raise ValueError(f"text must be 4–6 words (got {len(words)})")


def validate_image_prompt_strict(prompt: str) -> None:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("image_prompt must be non-empty")
    p = prompt.lower()

    # Must be vertical 9:16
    if "9:16" not in p:
        raise ValueError("image_prompt must mention 9:16")

    # Forbid style terms entirely (V2 must not look cinematic/stylized).
    hits = [t for t in FORBIDDEN_STYLE_TERMS if t in p]
    if hits:
        raise ValueError(f"image_prompt contains forbidden style terms: {hits}")

    # Must explicitly instruct no humans/animals.
    if "no people" not in p and "no human" not in p:
        raise ValueError("image_prompt must include 'no people'/'no human'")

    # Disallow any accidental explicit human/animal mentions outside negative sections.
    # Heuristic: remove the negative section then scan.
    scrubbed = p
    scrubbed = re.sub(r"strict negative:.*", "", scrubbed, flags=re.IGNORECASE | re.DOTALL)
    scrubbed = re.sub(r"content negative:.*", "", scrubbed, flags=re.IGNORECASE | re.DOTALL)

    # Allow explicit negations anywhere (common in well-formed prompts).
    for phrase in [
        "no people",
        "no person",
        "no human",
        "no face",
        "no animals",
        "no animal",
    ]:
        scrubbed = scrubbed.replace(phrase, "")

    hits2: list[str] = []
    for pat in FORBIDDEN_CONTENT_PATTERNS:
        if re.search(pat, scrubbed, flags=re.IGNORECASE):
            hits2.append(pat)
    if hits2:
        raise ValueError(f"image_prompt seems to mention forbidden content (patterns): {hits2}")
