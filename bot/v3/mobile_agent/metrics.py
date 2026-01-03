from __future__ import annotations

from dataclasses import dataclass

from .events import BaseEvent, OpenEvent, PauseEvent, ScrollEvent


@dataclass
class SessionMetrics:
    session_id: str
    started_ts: float

    last_event_ts: float
    scroll_count: int = 0
    open_count: int = 0
    pause_seconds: float = 0.0

    def elapsed_seconds(self, now_ts: float) -> float:
        return max(0.0, float(now_ts) - float(self.started_ts))

    def scroll_rate_per_min(self, now_ts: float) -> float:
        elapsed = self.elapsed_seconds(now_ts)
        if elapsed <= 0.0:
            return 0.0
        return float(self.scroll_count) / (elapsed / 60.0)

    def fatigue_score(self, now_ts: float) -> float:
        """Heuristic fatigue score in [0,1].

        Increases with session duration and aggressive scroll rate.
        """

        elapsed = self.elapsed_seconds(now_ts)
        rpm = self.scroll_rate_per_min(now_ts)

        # Normalize: 20 min => 1.0, 0 min => 0.0
        t = min(1.0, elapsed / (20.0 * 60.0))
        # Normalize: 60 scrolls/min => 1.0
        r = min(1.0, rpm / 60.0)
        # Pauses reduce fatigue a bit.
        pause_relief = min(0.3, float(self.pause_seconds) / 120.0)  # 2min pause => -0.3 max

        score = (0.55 * t) + (0.55 * r) - pause_relief
        return max(0.0, min(1.0, score))

    def apply_event(self, ev: BaseEvent) -> None:
        self.last_event_ts = float(ev.ts)
        if isinstance(ev, ScrollEvent):
            self.scroll_count += 1
        elif isinstance(ev, OpenEvent):
            self.open_count += 1
        elif isinstance(ev, PauseEvent):
            self.pause_seconds += float(ev.seconds)


def metrics_to_dict(m: SessionMetrics) -> dict:
    return {
        "session_id": m.session_id,
        "started_ts": float(m.started_ts),
        "last_event_ts": float(m.last_event_ts),
        "scroll_count": int(m.scroll_count),
        "open_count": int(m.open_count),
        "pause_seconds": float(m.pause_seconds),
    }


def dict_to_metrics(d: dict) -> SessionMetrics:
    sid = str(d.get("session_id") or "")
    started = float(d.get("started_ts") or 0.0)
    last = float(d.get("last_event_ts") or started)
    m = SessionMetrics(session_id=sid, started_ts=started, last_event_ts=last)
    m.scroll_count = int(d.get("scroll_count") or 0)
    m.open_count = int(d.get("open_count") or 0)
    m.pause_seconds = float(d.get("pause_seconds") or 0.0)
    return m
