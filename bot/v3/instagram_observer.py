from __future__ import annotations

from dataclasses import dataclass, field
import time


@dataclass
class ObservedVideo:
    """What V3 can observe about the current Instagram video.

    NOTE: Without accessibility/screen parsing, url is often unknown.
    We still persist immediately using a stable placeholder.
    """

    source: str = "instagram"
    source_url: str = ""  # best-effort, may be empty
    observed_at: float = 0.0
    meta: dict = field(default_factory=dict)


class InstagramObserver:
    """Stub observer.

    In a real implementation, this would use:
    - Accessibility service
    - UIAutomator
    - OCR / screen capture
    to extract creator handle, caption, audio name, and/or share URL.

    V3 keeps architecture ready while remaining dependency-light.
    """

    def observe_current(self) -> ObservedVideo:
        return ObservedVideo(
            source="instagram",
            source_url="",
            observed_at=float(time.time()),
            meta={},
        )
