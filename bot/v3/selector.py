from __future__ import annotations

from dataclasses import dataclass

from typing import Any


@dataclass(frozen=True)
class SelectionDecision:
    score: float
    should_keep: bool
    reason: str
    threshold: float
    details: dict

    # Parallel categorization outputs (do not affect should_keep)
    score_viral: float = 0.0
    score_latent: float = 0.0
    viral_label: str = ""  # "🔥 VIRAL" | "💎 LATENT" | "❌ IGNORER"


class Selector:
    """Simple, pluggable selection logic.

    'Intelligent' here is intentionally minimal: V3 is architecture-first.
    Replace this with a learned model / rules engine later.
    """

    def decide(self, obs: Any, *, settings: dict | None = None) -> SelectionDecision:
        """Return decision + explainable score details.

        V3 goal: align with V1/V2 moderation UX by showing:
        - score global
        - détails critères
        - seuil
        """

        meta = getattr(obs, "meta", {}) or {}
        s = dict(settings or {})

        # Inputs (0..1)
        rythme = float(meta.get("rythme") or 0.0)
        banalite = float(meta.get("banalite") or 0.0)
        potentiel_viral = float(meta.get("potentiel_viral") or 0.0)

        def _clamp01(x: float) -> float:
            return max(0.0, min(1.0, float(x)))

        def _score(*, w_b: float, w_p: float, w_r: float, target: float) -> float:
            w_b = float(w_b)
            w_p = float(w_p)
            w_r = float(w_r)
            target = _clamp01(float(target))
            s = (w_b * (1.0 - banalite)) + (w_p * potentiel_viral) + (w_r * (1.0 - abs(rythme - target)))
            return _clamp01(s)

        # --- EXISTING SCORE (defaults remain exactly as before) ---
        # Kept configurable via settings menu; reset returns to original.
        w_banalite = _clamp01(float(s.get("weight_banalite", 0.35) or 0.35))
        w_potentiel = _clamp01(float(s.get("weight_potentiel_viral", 0.35) or 0.35))
        w_rythme = _clamp01(float(s.get("weight_rythme", 0.30) or 0.30))
        rythme_target = _clamp01(float(s.get("rythme_target", 0.45) or 0.45))
        score = _score(w_b=w_banalite, w_p=w_potentiel, w_r=w_rythme, target=rythme_target)
        score = max(0.0, min(1.0, float(score)))

        threshold = _clamp01(float(s.get("score_threshold", 0.65) or 0.65))
        keep = score >= threshold

        # --- PARALLEL CATEGORIZATION (does NOT affect keep) ---
        thr_viral = _clamp01(float(s.get("threshold_viral", 0.72) or 0.72))
        v_wb = _clamp01(float(s.get("viral_w_banalite", 0.30) or 0.30))
        v_wp = _clamp01(float(s.get("viral_w_potentiel_viral", 0.45) or 0.45))
        v_wr = _clamp01(float(s.get("viral_w_rythme", 0.25) or 0.25))
        v_rt = _clamp01(float(s.get("viral_rythme_target", 0.50) or 0.50))
        score_viral = _score(w_b=v_wb, w_p=v_wp, w_r=v_wr, target=v_rt)

        thr_latent = _clamp01(float(s.get("threshold_latent", 0.60) or 0.60))
        l_wb = _clamp01(float(s.get("latent_w_banalite", 0.45) or 0.45))
        l_wp = _clamp01(float(s.get("latent_w_potentiel_viral", 0.20) or 0.20))
        l_wr = _clamp01(float(s.get("latent_w_rythme", 0.35) or 0.35))
        l_rt = _clamp01(float(s.get("latent_rythme_target", 0.38) or 0.38))
        score_latent = _score(w_b=l_wb, w_p=l_wp, w_r=l_wr, target=l_rt)

        if score_viral >= thr_viral:
            viral_label = "🔥 VIRAL"
        elif score_latent >= thr_latent:
            viral_label = "💎 LATENT"
        else:
            viral_label = "❌ IGNORER"

        # Human-readable reason.
        parts: list[str] = []
        if banalite >= 0.6:
            parts.append("anomalie quotidienne")
        if rythme < 0.45:
            parts.append("rythme lent")
        else:
            parts.append("rythme dynamique")
        if potentiel_viral >= 0.6:
            parts.append("potentiel viral")

        reason = " + ".join(parts) if parts else ("sélection" if keep else "non retenu")

        details = {
            "rythme": round(rythme, 3),
            "banalite": round(banalite, 3),
            "potentiel_viral": round(potentiel_viral, 3),
        }

        return SelectionDecision(
            score=score,
            should_keep=keep,
            reason=reason,
            threshold=threshold,
            details=details,
            score_viral=score_viral,
            score_latent=score_latent,
            viral_label=viral_label,
        )
