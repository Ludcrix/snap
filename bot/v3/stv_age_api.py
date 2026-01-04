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


def try_fetch_age_with_selenium(url: str) -> AgeApiResult | None:
    """Backward-compatible alias used by refresh flow.

    Prefer an external age API; fall back to the HTTP-based extractor implemented
    above. This function is intentionally conservative and non-blocking.
    """
    try:
        print(f"[AGE_API] try_fetch_age_with_selenium called url={url}", flush=True)
        res = try_fetch_age_seconds(url)
        print(f"[AGE_API] try_fetch_age_with_selenium result={res}", flush=True)
        return res
    except Exception as e:
        print(f"[AGE_API] try_fetch_age_with_selenium failed: {type(e).__name__}:{e}", flush=True)
        return None


def try_fetch_age_from_html(html: str) -> AgeApiResult | None:
    """Try to extract a created timestamp from rendered HTML content.

    Looks for common JSON fields such as `taken_at_timestamp` or `created_at`.
    """
    if not isinstance(html, str) or not html:
        return None
    try:
        # Look for unix timestamp fields
        import re

        m = re.search(r"\"taken_at_timestamp\"\s*:\s*(\d{9,12})", html)
        if m:
            try:
                ts = int(m.group(1))
                print(f"[AGE_API] found taken_at_timestamp={ts}", flush=True)
                now = datetime.now(timezone.utc)
                age = int((now - datetime.fromtimestamp(ts, tz=timezone.utc)).total_seconds())
                return AgeApiResult(age_seconds=max(age, 0), source="html:taken_at_timestamp")
            except Exception as e:
                print(f"[AGE_API] parse taken_at_timestamp failed: {type(e).__name__}:{e}", flush=True)
                pass

        # created_at ISO string
        m2 = re.search(r"\"created_at\"\s*:\s*\"([^\"]+)\"", html)
        if m2:
            try:
                s = m2.group(1)
                print(f"[AGE_API] found created_at={s}", flush=True)
                age2 = _parse_created_at_to_age_seconds(s)
                if age2 is not None:
                    return AgeApiResult(age_seconds=age2, source="html:created_at")
            except Exception as e:
                print(f"[AGE_API] parse created_at failed: {type(e).__name__}:{e}", flush=True)
                pass

        # Fallback: explicit AGE_SECONDS token injected by other tools
        m3 = re.search(r"AGE_SECONDS\s*[=]\s*(\d{1,10})", html)
        if m3:
            try:
                age3 = int(m3.group(1))
                print(f"[AGE_API] found AGE_SECONDS token={age3}", flush=True)
                return AgeApiResult(age_seconds=max(age3, 0), source="html:age_seconds_token")
            except Exception as e:
                print(f"[AGE_API] parse AGE_SECONDS token failed: {type(e).__name__}:{e}", flush=True)
                pass
    except Exception:
        return None
    return None


def try_fetch_metrics_from_html(html: str) -> dict | None:
    """Extract basic engagement metrics from rendered Instagram HTML/JSON.

    Returns a dict with keys like `likes`, `comments`, `views`, `sends`, `saves`, `remixes`
    when found, else None.
    """
    if not isinstance(html, str) or not html:
        return None
    try:
        import re

        got: dict = {}

        # likes: graphql.shortcode_media.edge_media_preview_like.count
        m_like = re.search(r'"edge_media_preview_like"\s*:\s*\{\s*"count"\s*:\s*(\d+)', html)
        if m_like:
            try:
                got["likes"] = int(m_like.group(1))
            except Exception:
                pass

        # comments: graphql.shortcode_media.edge_media_to_parent_comment.count
        m_com = re.search(r'"edge_media_to_parent_comment"\s*:\s*\{\s*"count"\s*:\s*(\d+)', html)
        if m_com:
            try:
                got["comments"] = int(m_com.group(1))
            except Exception:
                pass

        # views: video_view_count or display_resources patterns
        m_views = re.search(r'"video_view_count"\s*:\s*(\d+)', html)
        if m_views:
            try:
                got["views"] = int(m_views.group(1))
            except Exception:
                pass

        # Some pages include 'edge_media_to_...' or other counts; try to find generic counts
        if not got.get("likes"):
            m2 = re.search(r'"likes"\s*:\s*\{\s*"count"\s*:\s*(\d+)', html)
            if m2:
                try:
                    got["likes"] = int(m2.group(1))
                except Exception:
                    pass

        # If we found at least one metric, return
        if got:
            return got
    except Exception:
        return None
    return None


def fetch_created_time(url, **kwargs):
    print(f"[AGE][API] fetch_created_time called url={url} kwargs={kwargs}")
    res = None
    try:
        if 'try_fetch_age_with_selenium' in globals():
            try:
                res = try_fetch_age_with_selenium(url, **kwargs)
                print(f"[AGE][API] try_fetch_age_with_selenium -> {res}")
            except Exception as e:
                print(f"[AGE][API] selenium attempt failed: {e}")
        # If external age API didn't return anything, try fetching the raw HTML
        if not res and 'try_fetch_age_from_html' in globals():
            try:
                import urllib.request
                import urllib.error

                req = urllib.request.Request(str(url), headers={"User-Agent": "snap-bot/1.0"})
                try:
                    with urllib.request.urlopen(req, timeout=float(kwargs.get('timeout', 5.0))) as fh:
                        raw = fh.read() or b""
                    html = raw.decode('utf-8', errors='replace')
                except Exception as e:
                    print(f"[AGE][API] html fetch failed: {type(e).__name__}:{e}")
                    html = None

                if html:
                    try:
                        res = try_fetch_age_from_html(html)
                        print(f"[AGE][API] try_fetch_age_from_html -> {res}")
                    except Exception as e:
                        print(f"[AGE][API] html attempt failed: {type(e).__name__}:{e}")
            except Exception:
                pass

        # If still not found, try the programmatic Selenium helper from created_time_test
        if not res:
            try:
                from created_time_test import get_reel_age_seconds
                print(f"[AGE][API] calling created_time_test.get_reel_age_seconds url={url}")
                age_sec = get_reel_age_seconds(url)
                print(f"[AGE][API] created_time_test returned age_seconds={age_sec}")
                if isinstance(age_sec, int):
                    res = AgeApiResult(age_seconds=age_sec, source="selenium:created_time_test")
            except Exception as e:
                print(f"[AGE][API] created_time_test attempt failed: {type(e).__name__}:{e}")
    except Exception as e:
        print(f"[AGE][API] unexpected error: {e}")
    print(f"[AGE][API] fetch_created_time result for url={url} -> {res}")
    return res
