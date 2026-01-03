from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
import time


EventType = Literal["scroll", "pause", "open"]


@dataclass(frozen=True)
class BaseEvent:
    type: EventType
    ts: float
    session_id: str
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ScrollEvent(BaseEvent):
    # Positive delta means scrolling down.
    delta: int = 1


@dataclass(frozen=True)
class PauseEvent(BaseEvent):
    seconds: float = 0.0


@dataclass(frozen=True)
class OpenEvent(BaseEvent):
    target: str = ""  # e.g. a local simulated id / URL placeholder


def now_ts() -> float:
    return float(time.time())
