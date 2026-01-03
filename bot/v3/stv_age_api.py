from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class AgeApiResult:
    age_seconds: int
    source: str


def _parse_created_at_to_age_seconds(created_at: str) -> int | None:
    """Parse ISO8601 created_at and return age seconds (>=0), else None."""
    if not created_at:
        return None
    try:
        s = str(created_at).strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        age = int((now - dt.astimezone(timezone.utc)).total_seconds())
        return max(age, 0)
    except Exception:
        return None


def try_fetch_age_seconds(url: str) -> AgeApiResult | None:
    """Try to fetch a reliable age from an external endpoint before OCR.

    Configure via env:
      - V3_STV_AGE_API_URL: base URL, called as GET {url}?url=<reel_url>
      - V3_STV_AGE_API_TIMEOUT_S: float seconds (default 3.0)

    Expected JSON:
      - {"age_seconds": 12345}
      - {"created_at": "2026-01-02T19:47:59Z"}

    Returns AgeApiResult or None.
    """
    base = str(os.getenv("V3_STV_AGE_API_URL", "")).strip()
    if not base:
        return None

    try:
        timeout_s = float(str(os.getenv("V3_STV_AGE_API_TIMEOUT_S", "3.0")).strip() or "3.0")
    except Exception:
        timeout_s = 3.0

    query = urllib.parse.urlencode({"url": str(url)})
    full = base + ("&" if "?" in base else "?") + query

    req = urllib.request.Request(
        full,
        method="GET",
        headers={"User-Agent": "snap-bot-v3/1.0"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read() or b""
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    if "age_seconds" in data:
        try:
            age = int(data.get("age_seconds"))
            if age >= 0:
                return AgeApiResult(age_seconds=age, source="age_api:age_seconds")
        except Exception:
            pass

    if isinstance(data.get("created_at"), str):
        age2 = _parse_created_at_to_age_seconds(str(data.get("created_at")))
        if age2 is not None:
            return AgeApiResult(age_seconds=age2, source="age_api:created_at")

    return None
