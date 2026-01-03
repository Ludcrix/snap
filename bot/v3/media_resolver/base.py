from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


MediaResolutionStatus = Literal["RESOLVED", "RESOLUTION_REQUIRED", "FAILED"]


@dataclass(frozen=True)
class MediaResolution:
    status: MediaResolutionStatus
    source_url: str
    local_path: str | None
    message: str


class BaseMediaResolver:
    """Interchangeable media resolver.

    Goals:
    - Associate each kept video with a source URL identity (not a direct file link).
    - Produce a local working media file ONLY for pertinent videos.
    - Log every resolution attempt.

    Constraints:
    - Must not scrape Instagram.
    - Must not perform massive actions.
    - If resolution fails: keep URL, mark "resolution required", do not block session.
    """

    def resolve(self, *, source_url: str, video_id: str, out_dir: str) -> MediaResolution:
        raise NotImplementedError
