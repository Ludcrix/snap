from __future__ import annotations


def generate_title(*, score: float, reason: str, source_url: str) -> str:
    # Minimal, deterministic-ish title for V3.
    base = reason.strip() or "Sélection détectée"
    if len(base) > 70:
        base = base[:67] + "…"
    # Add a short hint of the score.
    return f"{base} (score {score:.2f})"


def generate_hashtags(*, score_details: dict) -> list[str]:
    # Simple mapping; keeps output stable and short.
    tags: list[str] = ["#reels", "#analyse", "#snapbot"]

    def _add(t: str):
        if t not in tags:
            tags.append(t)

    rhythm = float(score_details.get("rythme") or 0.0)
    banality = float(score_details.get("banalite") or 0.0)
    viral = float(score_details.get("potentiel_viral") or 0.0)

    if rhythm < 0.4:
        _add("#rythmelent")
    elif rhythm > 0.7:
        _add("#rythmerapide")

    if banality > 0.65:
        _add("#quotidien")

    if viral > 0.65:
        _add("#viral")

    return tags[:8]
