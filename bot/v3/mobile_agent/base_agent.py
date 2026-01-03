from __future__ import annotations

from abc import ABC, abstractmethod

from .events import OpenEvent, PauseEvent, ScrollEvent


class BaseMobileAgent(ABC):
    """Abstract interface for a mobile agent.

    IMPORTANT (V3 Step2):
    - This interface is meant to be implemented by a simulated agent today.
    - Later, a real agent can be plugged in WITHOUT changing the rest of V3.

    This must NOT directly automate third-party platforms in Step2.
    """

    @abstractmethod
    def start_session(self, session_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def stop_session(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def scroll(self) -> ScrollEvent:
        raise NotImplementedError

    @abstractmethod
    def pause(self, seconds: float) -> PauseEvent:
        raise NotImplementedError

    @abstractmethod
    def open(self) -> OpenEvent:
        raise NotImplementedError
