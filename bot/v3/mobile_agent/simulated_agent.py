from __future__ import annotations

from dataclasses import dataclass
import random

from .base_agent import BaseMobileAgent
from .events import OpenEvent, PauseEvent, ScrollEvent, now_ts


@dataclass
class SimulatedAgentConfig:
    seed: int | None = None
    # Probability that a scroll results in an "open" action.
    open_probability: float = 0.15
    # Typical pause duration after opening.
    open_pause_seconds: tuple[float, float] = (1.5, 4.0)
    # Size of the simulated "content pool". A smaller pool means you'll naturally
    # see the same videos again.
    content_pool_size: int = 200


class SimulatedMobileAgent(BaseMobileAgent):
    """Pure simulation. No ADB. No UI automation.

    Generates a plausible stream of scroll/pause/open events.
    """

    def __init__(self, cfg: SimulatedAgentConfig | None = None):
        self._cfg = cfg or SimulatedAgentConfig()
        self._rng = random.Random(self._cfg.seed)
        self._session_id: str | None = None
        self._content_pool: list[str] = []
        self._current_content_key: str | None = None

    def start_session(self, session_id: str) -> None:
        self._session_id = str(session_id)
        # Build a deterministic pool so repeats can happen.
        n = int(self._cfg.content_pool_size)
        n = max(10, min(5000, n))
        # Use opaque ids; SessionManager will map them to IG-like URLs.
        self._content_pool = [f"sim_content_{self._rng.randint(100000, 999999)}" for _ in range(n)]
        self._current_content_key = None

    def stop_session(self) -> None:
        self._session_id = None
        self._content_pool = []
        self._current_content_key = None

    def _pick_content_key(self) -> str:
        if not self._content_pool:
            return f"sim_content_{self._rng.randint(100000, 999999)}"
        return self._content_pool[self._rng.randrange(len(self._content_pool))]

    def _sid(self) -> str:
        if not self._session_id:
            raise RuntimeError("SimulatedMobileAgent session not started")
        return self._session_id

    def scroll(self) -> ScrollEvent:
        self._current_content_key = self._pick_content_key()
        return ScrollEvent(
            type="scroll",
            ts=now_ts(),
            session_id=self._sid(),
            meta={"content_key": self._current_content_key},
            delta=1,
        )

    def pause(self, seconds: float) -> PauseEvent:
        sec = max(0.0, float(seconds))
        return PauseEvent(type="pause", ts=now_ts(), session_id=self._sid(), meta={}, seconds=sec)

    def open(self) -> OpenEvent:
        # In simulation, we don't have a real URL, but we DO keep a stable identity.
        if self._current_content_key is None:
            self._current_content_key = self._pick_content_key()
        target = f"sim://reel/{self._current_content_key}"
        return OpenEvent(
            type="open",
            ts=now_ts(),
            session_id=self._sid(),
            meta={"content_key": self._current_content_key},
            target=target,
        )

    def should_open_after_scroll(self) -> bool:
        p = max(0.0, min(1.0, float(self._cfg.open_probability)))
        return self._rng.random() < p

    def choose_open_pause_seconds(self) -> float:
        a, b = self._cfg.open_pause_seconds
        lo = min(float(a), float(b))
        hi = max(float(a), float(b))
        return lo + (hi - lo) * self._rng.random()
