from __future__ import annotations

import re
import unicodedata
import random


_ANOMALY_TO_TITLE_TEMPLATE: dict[str, str] = {
    "une ombre qui ne correspond pas à l'objet": "{obj} avait une ombre bizarre",
    "un reflet qui montre un angle impossible": "{obj} avait un reflet faux",
    "un minuscule détail qui bouge alors que tout est immobile": "{obj} bougeait un peu",
    "une étiquette avec une date qui change entre deux instants": "{obj} avait une date différente",
    "une petite partie de l'objet qui semble légèrement décalée": "{obj} était légèrement décalé",
    "un coin de l'objet qui paraît trop net, comme découpé": "{obj} avait un coin trop net",
    "une marque de doigt qui apparaît puis disparaît": "{obj} avait une trace fugace",
    "un motif répétitif qui n'est pas cohérent": "{obj} avait un motif incohérent",
    "une fissure qui n'était pas là une seconde avant": "{obj} avait une fissure nouvelle",
    "un élément du décor qui se répète exactement deux fois": "Le décor se répétait",
}


def _strip_french_article(s: str) -> str:
    s = str(s or "").strip()
    if not s:
        return s
    lowered = s.lower()
    for prefix in ("un ", "une ", "des ", "du ", "de la ", "de l'", "le ", "la ", "les "):
        if lowered.startswith(prefix):
            return s[len(prefix) :].strip()
    return s


def make_snap_title(*, obj: str, anomaly: str) -> str:
    """Short factual hook title based on object + anomaly (no narration)."""

    obj_clean = _strip_french_article(obj)
    template = _ANOMALY_TO_TITLE_TEMPLATE.get(str(anomaly or "").strip())

    if template:
        title = template.format(obj=obj_clean)
    else:
        # Fallback: keep it factual, short, and grounded.
        title = f"{obj_clean} avait un détail étrange" if obj_clean else "Détail étrange"

    title = re.sub(r"\s+", " ", title).strip()
    # Keep it reasonably short for Telegram list readability.
    return title[:80].rstrip()


def _slugify_tag(text: str) -> str:
    t = str(text or "").strip().lower()
    if not t:
        return ""
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = re.sub(r"[^a-z0-9]+", "", t)
    return t


def make_hashtags(*, obj: str, anomaly: str) -> list[str]:
    """Snap-Spotlight oriented hashtags (generic + behavioral + banal curiosity)."""

    # Keep this within 5–10 tags as requested.
    base = [
        "#snap",
        "#spotlight",
        "#anomalie",
        "#objet",
        "#detail",
        "#etrange",
        "#bizarre",
        "#curiosite",
    ]

    obj_tag = _slugify_tag(_strip_french_article(obj))
    if obj_tag:
        base.append(f"#{obj_tag}")

    # Light anomaly-derived tags (do not overfit; keep generic).
    a = str(anomaly or "").lower()
    if "ombre" in a:
        base.append("#ombre")
    if "reflet" in a:
        base.append("#reflet")
    if "fissure" in a:
        base.append("#fissure")
    if ("date" in a or "étiquette" in a or "etiquette" in a) and "#detail" not in base:
        base.append("#detail")

    # Deduplicate while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for t in base:
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)

    # Enforce 5–10, preserving order.
    if len(out) < 5:
        # Should not happen, but keep safe.
        out = (out + ["#snap", "#spotlight", "#anomalie", "#objet", "#detail"])[:5]
    return out[:10]


def make_snap_hook(*, obj: str, anomaly: str, rng: random.Random | None = None) -> str:
    """Short neutral one-liner for Snap message (NOT in-video).

    Constraints:
    - one short sentence
    - factual / neutral
    - no hype, no promise, no sensationalism
    - 0–2 emojis max
    """

    r = rng or random.Random()
    obj_clean = _strip_french_article(obj)
    obj_clean = re.sub(r"\s+", " ", obj_clean).strip()

    # Keep them generic and grounded.
    templates = [
        "C'était déjà comme ça{emoji}",
        "Rien n'avait bougé{emoji}",
        "Juste un détail{emoji}",
        "Je l'avais pas remarqué{emoji}",
        "Sur {obj}, un détail{emoji}",
    ]
    t = r.choice(templates)

    emojis = ["", " 👀", " 🤏"]
    # Allow at most one emoji by default; occasionally none.
    emoji = r.choice(emojis)

    if "{obj}" in t and not obj_clean:
        t = "Juste un détail{emoji}"

    out = t.format(obj=obj_clean, emoji=emoji)
    out = out.strip()
    out = re.sub(r"\s+", " ", out)
    out = out.rstrip(".!?")
    return out
