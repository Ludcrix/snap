from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .metrics import SessionMetrics


RiskLevel = Literal["SAFE", "WARNING", "HIGH_RISK"]


@dataclass(frozen=True)
class RiskAssessment:
    level: RiskLevel
    justification: str
    remaining_seconds: float


class RiskEstimator:
    """Heuristic risk estimator.

    Goal: detect overly long or overly aggressive sessions.
    Used to stop sessions automatically when risk is HIGH_RISK.
    """

    def __init__(self, *, max_session_seconds: float = 15 * 60):
        # Recommended max is 15 minutes, but keep a sane floor.
        self._max = max(60.0, float(max_session_seconds))

    def _log(self, *, reason: str, metric: str, threshold: str) -> None:
        # Explicit, grep-friendly logs.
        # NOTE: kept as print() to avoid relying on logging configuration.
        print(f"[RISK] reason={reason} metric={metric} threshold={threshold}")

    def _per_min(self, count: float, elapsed_seconds: float) -> float:
        if elapsed_seconds <= 0.0:
            return 0.0
        return float(count) / (float(elapsed_seconds) / 60.0)

    def assess(self, m: SessionMetrics, *, now_ts: float) -> RiskAssessment:
        elapsed = m.elapsed_seconds(now_ts)
        remaining = max(0.0, self._max - elapsed)
        fatigue = m.fatigue_score(now_ts)
        scroll_rpm = m.scroll_rate_per_min(now_ts)
        open_rpm = self._per_min(m.open_count, elapsed)
        pause_per_min = self._per_min(m.pause_seconds, elapsed)
        open_per_scroll = float(m.open_count) / float(max(1, m.scroll_count))

        # Warmup: during the first minute, rate metrics can be wildly inflated
        # (small denominator). We still compute them for logging, but we do not
        # trigger rate-based WARNING/HIGH_RISK solely from them.
        in_warmup = elapsed < 60.0

        # --- HARD STOPS (HIGH_RISK) ---
        # Objective must NEVER override risk: hard stops are immediate.

        if elapsed >= self._max:
            self._log(
                reason="max_session_seconds_reached",
                metric=f"elapsed_s={elapsed:.1f}",
                threshold=f">={self._max:.1f}",
            )
            return RiskAssessment(
                level="HIGH_RISK",
                justification="temps_max_session_atteint",
                remaining_seconds=0.0,
            )

        # High risk: very high fatigue.
        if fatigue >= 0.90:
            self._log(
                reason="fatigue_too_high",
                metric=f"fatigue={fatigue:.2f}",
                threshold=">=0.90",
            )
            return RiskAssessment(
                level="HIGH_RISK",
                justification=f"fatigue={fatigue:.2f}",
                remaining_seconds=remaining,
            )

        # High risk: extreme scrolling intensity.
        if (not in_warmup) and scroll_rpm >= 90.0:
            self._log(
                reason="scroll_rate_extreme",
                metric=f"scroll_rpm={scroll_rpm:.1f}",
                threshold=">=90.0",
            )
            return RiskAssessment(
                level="HIGH_RISK",
                justification=f"scroll_rpm={scroll_rpm:.1f}, fatigue={fatigue:.2f}",
                remaining_seconds=remaining,
            )

        # High risk: compulsive pattern (high scroll rate + almost no pauses) after a few minutes.
        if (not in_warmup) and elapsed >= 5 * 60 and scroll_rpm >= 75.0 and pause_per_min <= 0.5:
            self._log(
                reason="high_scroll_low_pause",
                metric=f"scroll_rpm={scroll_rpm:.1f}; pause_per_min={pause_per_min:.2f}",
                threshold="scroll_rpm>=75.0 and pause_per_min<=0.50 (after 5min)",
            )
            return RiskAssessment(
                level="HIGH_RISK",
                justification=f"scroll_rpm={scroll_rpm:.1f}, pause_per_min={pause_per_min:.2f}",
                remaining_seconds=remaining,
            )

        # High risk: too many opens per minute (rapid content hopping) with low pauses.
        if (not in_warmup) and elapsed >= 5 * 60 and open_rpm >= 20.0 and pause_per_min <= 0.8:
            self._log(
                reason="open_rate_extreme_low_pause",
                metric=f"open_rpm={open_rpm:.1f}; pause_per_min={pause_per_min:.2f}",
                threshold="open_rpm>=20.0 and pause_per_min<=0.80 (after 5min)",
            )
            return RiskAssessment(
                level="HIGH_RISK",
                justification=f"open_rpm={open_rpm:.1f}, pause_per_min={pause_per_min:.2f}",
                remaining_seconds=remaining,
            )

        # --- WARNINGS ---
        # Warning: nearing time limit.
        if remaining <= 180.0:
            self._log(
                reason="near_time_limit",
                metric=f"remaining_s={remaining:.1f}",
                threshold="<=180.0",
            )
            return RiskAssessment(
                level="WARNING",
                justification=f"remaining_s={remaining:.0f}",
                remaining_seconds=remaining,
            )

        # Warning: elevated fatigue.
        if fatigue >= 0.70:
            self._log(
                reason="fatigue_elevated",
                metric=f"fatigue={fatigue:.2f}",
                threshold=">=0.70",
            )
            return RiskAssessment(
                level="WARNING",
                justification=f"fatigue={fatigue:.2f}",
                remaining_seconds=remaining,
            )

        # Warning: high scroll intensity (even if not extreme).
        if (not in_warmup) and scroll_rpm >= 60.0:
            self._log(
                reason="scroll_rate_high",
                metric=f"scroll_rpm={scroll_rpm:.1f}",
                threshold=">=60.0",
            )
            return RiskAssessment(
                level="WARNING",
                justification=f"scroll_rpm={scroll_rpm:.1f}",
                remaining_seconds=remaining,
            )

        # Warning: high open rate per minute (content hopping).
        if (not in_warmup) and open_rpm >= 12.0:
            self._log(
                reason="open_rate_high",
                metric=f"open_rpm={open_rpm:.1f}",
                threshold=">=12.0",
            )
            return RiskAssessment(
                level="WARNING",
                justification=f"open_rpm={open_rpm:.1f}",
                remaining_seconds=remaining,
            )

        # Warning: low pauses sustained (insufficient recovery) after a few minutes.
        if (not in_warmup) and elapsed >= 8 * 60 and pause_per_min <= 0.8:
            self._log(
                reason="pause_rate_low",
                metric=f"pause_per_min={pause_per_min:.2f}",
                threshold="<=0.80 (after 8min)",
            )
            return RiskAssessment(
                level="WARNING",
                justification=f"pause_per_min={pause_per_min:.2f}",
                remaining_seconds=remaining,
            )

        # --- SAFE ---
        # Safe is still logged (explicit signal), but indicates no threshold hit.
        self._log(
            reason="within_limits",
            metric=(
                f"elapsed_s={elapsed:.0f}; scroll_rpm={scroll_rpm:.1f}; open_rpm={open_rpm:.1f}; "
                f"pause_per_min={pause_per_min:.2f}; open_per_scroll={open_per_scroll:.2f}; fatigue={fatigue:.2f}"
            ),
            threshold=(
                f"max_s={self._max:.0f}; warn_remaining<=180; warn_fatigue>=0.70; warn_scroll_rpm>=60; "
                f"warn_open_rpm>=12; warn_pause_per_min<=0.80(after8m); high_fatigue>=0.90; high_scroll_rpm>=90"
            ),
        )

        return RiskAssessment(
            level="SAFE",
            justification=(
                f"elapsed_s={elapsed:.0f}, scroll_rpm={scroll_rpm:.1f}, open_rpm={open_rpm:.1f}, "
                f"pause_per_min={pause_per_min:.2f}, fatigue={fatigue:.2f}"
            ),
            remaining_seconds=remaining,
        )
