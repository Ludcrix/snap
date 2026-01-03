from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import re
from typing import Any


@dataclass(frozen=True)
class TemporalAnalysis:
    t_capture_utc: datetime
    t_pub_utc: datetime | None
    age_minutes: float | None

    likes: int | None
    shares: int | None
    sends: int | None
    saves: int | None
    remixes: int | None
    comments: int | None
    views: int | None

    like_velocity: float | None
    share_velocity: float | None
    send_velocity: float | None
    save_velocity: float | None
    remix_velocity: float | None
    comment_velocity: float | None

    stv: float | None
    category: str

    ocr_source: str
    ocr_pub_raw: str | None
    notes: str = ""


_REL_TIME_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # FR
    (re.compile(r"\bil y a\s+(\d+)\s*(min|minute|minutes)\b", re.IGNORECASE), "minutes"),
    (re.compile(r"\bil y a\s+(\d+)\s*(h|heure|heures)\b", re.IGNORECASE), "hours"),
    (re.compile(r"\bil y a\s+(\d+)\s*(j|jour|jours)\b", re.IGNORECASE), "days"),
    (re.compile(r"\bil y a\s+(\d+)\s*(sem|semaine|semaines)\b", re.IGNORECASE), "weeks"),
    # FR compact (often rendered without "il y a" on some UIs)
    (re.compile(r"(?:^|[^0-9A-Za-z])(?:·|•)?\s*(\d+)\s*(?:min|mins)\b", re.IGNORECASE), "minutes"),
    (re.compile(r"(?:^|[^0-9A-Za-z])(?:·|•)?\s*(\d+)\s*(?:h|heures?)\b", re.IGNORECASE), "hours"),
    (re.compile(r"(?:^|[^0-9A-Za-z])(?:·|•)?\s*(\d+)\s*(?:j|jours?)\b", re.IGNORECASE), "days"),
    (re.compile(r"(?:^|[^0-9A-Za-z])(?:·|•)?\s*(\d+)\s*(?:sem|semaines?)\b", re.IGNORECASE), "weeks"),
    # EN
    (re.compile(r"\b(\d+)\s*(minute|minutes)\s+ago\b", re.IGNORECASE), "minutes"),
    (re.compile(r"\b(\d+)\s*(hour|hours)\s+ago\b", re.IGNORECASE), "hours"),
    (re.compile(r"\b(\d+)\s*(day|days)\s+ago\b", re.IGNORECASE), "days"),
    (re.compile(r"\b(\d+)\s*(week|weeks)\s+ago\b", re.IGNORECASE), "weeks"),
]


def _env_float(name: str, default: float) -> float:
    v = str(os.getenv(name, "")).strip()
    if not v:
        return float(default)
    try:
        return float(v)
    except Exception:
        return float(default)


def parse_relative_pub_time(raw_text: str, *, t_capture_utc: datetime) -> tuple[datetime | None, float | None, str]:
    """Parse OCR text for an explicit relative publication time.

    IMPORTANT: no approximations.
    Accept only explicit numeric + unit patterns like:
      - "il y a 12 min", "il y a 2 h", "il y a 1 j"
      - "12 minutes ago", "2 hours ago"

    Returns: (t_pub_utc, age_minutes, reason)
    """
    if not raw_text:
        return None, None, "no_text"

    # Optional override injected by STV refresh (age fetched from external API).
    m_age = re.search(r"\bAGE_SECONDS\s*=\s*(\d{1,10})\b", raw_text)
    if m_age:
        try:
            age_s = int(m_age.group(1))
            if age_s >= 0:
                age_min = float(age_s) / 60.0
                t_pub = t_capture_utc - timedelta(seconds=age_s)
                return t_pub, age_min, "matched:age_seconds"
        except Exception:
            pass

    for rx, unit in _REL_TIME_PATTERNS:
        m = rx.search(raw_text)
        if not m:
            continue
        try:
            n = int(m.group(1))
        except Exception:
            continue

        if unit == "minutes":
            delta = timedelta(minutes=n)
            age_min = float(n)
        elif unit == "hours":
            delta = timedelta(hours=n)
            age_min = float(n) * 60.0
        elif unit == "days":
            delta = timedelta(days=n)
            age_min = float(n) * 1440.0
        elif unit == "weeks":
            delta = timedelta(days=7 * n)
            age_min = float(n) * 10080.0
        else:
            continue

        t_pub = t_capture_utc - delta
        return t_pub, age_min, f"matched:{unit}"

    return None, None, "no_match"


def parse_compact_count(s: str) -> int | None:
    """Parse OCR counts like "1 240", "1.2k", "1,2M".

    Conservative: returns None if it can't confidently parse.
    """
    if not s:
        return None
    s = str(s).strip()
    s = s.replace("\u202f", " ").replace("\xa0", " ")
    s_no_space = re.sub(r"\s+", "", s)

    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)([kKmM])?", s_no_space)
    if not m:
        m2 = re.search(r"(\d+(?:[.,]\d+)?)(\s*[kKmM])?", s)
        if not m2:
            return None
        num = m2.group(1)
        suf = (m2.group(2) or "").strip()
    else:
        num = m.group(1)
        suf = (m.group(2) or "").strip()

    try:
        num_f = float(str(num).replace(",", "."))
    except Exception:
        return None

    mult = 1
    if suf.lower() == "k":
        mult = 1000
    elif suf.lower() == "m":
        mult = 1_000_000

    try:
        return int(round(num_f * mult))
    except Exception:
        return None


def extract_metrics_from_ocr_text(raw_text: str) -> dict[str, int | None]:
    """Best-effort extraction of visible counters from OCR text.

    Priority order:
      1) If labels are present (likes/j'aime, commentaires, partages, vues), use them.
      2) Otherwise, try a conservative layout-based fallback on visible numeric tokens.

    The fallback is intended for typical Reel overlays where counts are shown but
    labels are not reliably OCR'd. It may still return None when ambiguous.
    """
    if not raw_text:
        return {"likes": None, "shares": None, "sends": None, "saves": None, "remixes": None, "comments": None, "views": None}

    text = str(raw_text)

    def _extract_section(tag: str) -> str:
        # Sections are appended as:
        #   [TAG]\n...text...\n\n[OTHER]...
        i = text.find(tag)
        if i < 0:
            return ""
        j = text.find("\n\n[", i + len(tag))
        if j < 0:
            return text[i + len(tag) :]
        return text[i + len(tag) : j]

    def _extract_counts_in_order(section_text: str) -> list[int]:
        # Read counts in appearance order (top-to-bottom). We rely on the
        # right icon column having a stable vertical order.
        if not section_text:
            return []
        out: list[int] = []

        # Prefer line-based extraction: each icon row usually yields one line.
        # Note: OCR sometimes merges two rows into one line; we extract *all* count tokens per line.
        for line in str(section_text).splitlines():
            s = line.strip()
            if not s:
                continue
            for m in re.finditer(r"\b\d[\d\s.,]*\s*[kKmM]?\b", s):
                tok = (m.group(0) or "").strip()
                if not tok or ":" in tok or "%" in tok:
                    continue
                v = parse_compact_count(tok)
                if v is None:
                    continue
                out.append(int(v))

        # Fallback: if OCR didn't split into lines, scan entire blob.
        if not out:
            for m in re.finditer(r"\b\d[\d\s.,]*\s*[kKmM]?\b", section_text):
                tok = (m.group(0) or "").strip()
                if not tok or ":" in tok or "%" in tok:
                    continue
                v = parse_compact_count(tok)
                if v is None:
                    continue
                out.append(int(v))

        # De-dupe sequential repeats (OCR sometimes repeats the same number).
        deduped: list[int] = []
        for v in out:
            if not deduped or deduped[-1] != v:
                deduped.append(v)
        return deduped

    # 1) Strongest signal: right column (icons + counters).
    right = _extract_section("[OCR_RIGHT_COLUMN]\n")
    if right.strip():
        ordered = _extract_counts_in_order(right)
        # Expected order (as per your UI):
        #   0 likes (heart)
        #   1 comments
        #   2 remixes
        #   3 sends
        #   4 saves
        likes = ordered[0] if len(ordered) >= 1 else None
        comments = ordered[1] if len(ordered) >= 2 else None
        remixes = ordered[2] if len(ordered) >= 3 else None
        sends = ordered[3] if len(ordered) >= 4 else None
        saves = ordered[4] if len(ordered) >= 5 else None
        # Keep shares as alias of sends only when we don't have explicit shares.
        shares = sends
        # Views are not part of the right column mapping (too unreliable), keep None here.
        views = None
        return {
            "likes": likes,
            "shares": shares,
            "sends": sends,
            "saves": saves,
            "remixes": remixes,
            "comments": comments,
            "views": views,
        }

    # Avoid common OCR noise patterns that create fake "counts".
    # - times (20:16)
    # - resolutions (1080x1920)
    scrubbed = re.sub(r"\b\d{1,2}:\d{2}\b", " ", text)
    scrubbed = re.sub(r"\b\d+\s*[x×]\s*\d+\b", " ", scrubbed, flags=re.IGNORECASE)

    def _extract_candidate_tokens(s: str) -> list[tuple[str, int]]:
        # Capture things like: 253K, 12,4K, 10,5 K, 1 240, 1.3M
        # Exclude percentages and obviously non-count artifacts.
        out: list[tuple[str, int]] = []
        for m in re.finditer(r"\b\d[\d\s.,]*\s*[kKmM]?\b", s):
            tok = (m.group(0) or "").strip()
            if not tok:
                continue
            if ":" in tok or "%" in tok:
                continue
            # skip single-digit noise unless it had a suffix (k/m)
            has_suffix = bool(re.search(r"[kKmM]", tok))
            v = parse_compact_count(tok)
            if v is None:
                continue
            if v < 10 and not has_suffix:
                continue
            out.append((tok, v))

        # De-duplicate (same number repeated by OCR).
        uniq: dict[int, str] = {}
        for tok, v in out:
            if v not in uniq:
                uniq[v] = tok
        # Keep stable ordering by appearance where possible.
        deduped: list[tuple[str, int]] = []
        seen: set[int] = set()
        for tok, v in out:
            if v in seen:
                continue
            seen.add(v)
            deduped.append((tok, v))
        return deduped

    def _m(pat: str) -> int | None:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if not m:
            return None
        return parse_compact_count(m.group(1) or "")

    # 2) Label-based extraction (when OCR captures words).
    likes = _m(r"(\d[\d\s.,]*[kKmM]?)\s*(j[' ]?aime|likes?)\b")
    shares = _m(r"(\d[\d\s.,]*[kKmM]?)\s*(partages?|shares?)\b")
    sends = _m(r"(\d[\d\s.,]*[kKmM]?)\s*(envois?|envoy[ée]s?|sent|sends?)\b")
    saves = _m(r"(\d[\d\s.,]*[kKmM]?)\s*(enregistr(?:ements?)?|sauvegard(?:es?)?|saved|saves?)\b")
    remixes = _m(r"(\d[\d\s.,]*[kKmM]?)\s*(remix(?:ages?)?|remixes?)\b")
    comments = _m(r"(\d[\d\s.,]*[kKmM]?)\s*(commentaires?|comments?)\b")
    views = _m(r"(\d[\d\s.,]*[kKmM]?)\s*(vues?|views?)\b")

    # Layout-based fallback: when labels aren't captured but numbers are.
    # Heuristics:
    # - if we have 4 candidates and one is much larger, treat it as views
    # - remaining 3 map as likes (max), comments (mid), shares (min)
    # - if only 3 candidates, map them as likes/comments/shares
    if all(x is None for x in (likes, shares, sends, saves, remixes, comments, views)):
        candidates = _extract_candidate_tokens(scrubbed)
        if candidates:
            values = [v for _, v in candidates]
            values_sorted = sorted(values, reverse=True)

            def _pop_value(target: int) -> None:
                nonlocal values
                values = [v for v in values if v != target]

            if len(values_sorted) >= 4:
                v0, v1 = values_sorted[0], values_sorted[1]
                # If the largest is clearly an outlier, assume it's views.
                if v1 > 0 and (v0 >= 3 * v1 or v0 >= 1_000_000 > v1):
                    views = v0
                    _pop_value(v0)
                    values_sorted = sorted(values, reverse=True)

            # Remaining mapping is heuristic and conservative.
            # Typical order on Reels: likes (largest), comments, sends/shares, saves, remixes.
            # OCR often misses some of them; we fill what we can from biggest to smallest.
            if len(values_sorted) >= 1:
                likes = values_sorted[0]
            if len(values_sorted) >= 2:
                comments = values_sorted[1]
            if len(values_sorted) >= 3:
                # Prefer "sends" for the 3rd slot; keep shares as a fallback alias.
                sends = values_sorted[2]
                shares = shares or sends
            if len(values_sorted) >= 4:
                saves = values_sorted[3]
            if len(values_sorted) >= 5:
                remixes = values_sorted[4]

    return {
        "likes": likes,
        "shares": shares,
        "sends": sends,
        "saves": saves,
        "remixes": remixes,
        "comments": comments,
        "views": views,
    }


def _as_int_or_none(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        try:
            return int(v)
        except Exception:
            return None
    s = str(v).strip()
    if not s:
        return None
    # allow compact counts like "1.2k"
    parsed = parse_compact_count(s)
    if parsed is not None:
        return parsed
    # fallback: digits-only
    try:
        s2 = re.sub(r"[^0-9]", "", s)
        return int(s2) if s2 else None
    except Exception:
        return None


def compute_stv(
    *,
    likes: int,
    shares: int,
    comments: int,
    age_minutes: float,
    sends: int | None = None,
    saves: int | None = None,
    remixes: int | None = None,
) -> tuple[float, float, float, float, float | None, float | None, float | None]:
    if age_minutes <= 0.0:
        raise ValueError("age_minutes must be > 0")
    like_v = likes / age_minutes
    share_v = shares / age_minutes
    comment_v = comments / age_minutes

    send_v = (sends / age_minutes) if isinstance(sends, int) else None
    save_v = (saves / age_minutes) if isinstance(saves, int) else None
    remix_v = (remixes / age_minutes) if isinstance(remixes, int) else None

    # Backward-compatible core STV.
    stv = (like_v * 0.45) + (share_v * 0.40) + (comment_v * 0.15)

    # If we have richer metrics, blend them in (still best-effort).
    # Weights are intentionally conservative to avoid exploding scores.
    if send_v is not None or save_v is not None or remix_v is not None:
        stv2 = (like_v * 0.30) + (comment_v * 0.10)
        if send_v is not None:
            stv2 += (send_v * 0.30)
        else:
            stv2 += (share_v * 0.30)
        if save_v is not None:
            stv2 += (save_v * 0.20)
        if remix_v is not None:
            stv2 += (remix_v * 0.10)
        # keep within reasonable range by averaging with legacy score
        stv = 0.5 * stv + 0.5 * stv2

    return like_v, share_v, comment_v, stv, send_v, save_v, remix_v


def classify(*, age_minutes: float | None, likes: int | None, comments: int | None, stv: float | None) -> str:
    # Must return exactly one category from the required set.
    if age_minutes is None or stv is None:
        return "🟡 Normale"

    # Adjustable thresholds via env (easy for humans)
    prev_viral_stv_min = _env_float("V3_STV_PREVIRAL_MIN", 3.0)
    promising_stv_min = _env_float("V3_STV_PROMISING_MIN", 1.2)
    underperf_stv_max = _env_float("V3_STV_UNDERPERF_MAX", 0.25)

    # Déjà explosée
    if age_minutes > 360.0 and ((likes or 0) >= 100_000 or (comments or 0) >= 5_000):
        return "🔥 Déjà explosée"

    # Pré-virale (phase test)
    if age_minutes < 120.0 and stv >= prev_viral_stv_min:
        return "🚀 Pré-virale (phase test)"

    if stv >= promising_stv_min:
        return "🟠 Prometteuse"

    if stv <= underperf_stv_max:
        return "🟢 Sous-performante"

    return "🟡 Normale"


def analyze_from_meta(*, meta: dict, t_capture_utc: datetime | None = None) -> TemporalAnalysis:
    """Purely additive analysis.

    Reads OCR-derived fields from meta if present; never writes anything.
    No approximations: if pub time isn't explicitly parsed, age/stv stays None.

    Accepted meta keys (best-effort):
      - ocr_pub / ocr_pub_text / ocr_age_text / ocr_text / ocr_raw_text
    - ocr_metrics: {likes, shares, sends, saves, remixes, comments, views}
    - ocr_likes / ocr_shares / ocr_sends / ocr_saves / ocr_remixes / ocr_comments / ocr_views
    """
    t_capture_utc = t_capture_utc or datetime.now(timezone.utc)

    ocr_text = None
    for k in ("ocr_pub", "ocr_pub_text", "ocr_age_text", "ocr_text", "ocr_raw_text", "ui_ocr_text"):
        v = meta.get(k)
        if isinstance(v, str) and v.strip():
            ocr_text = v.strip()
            break

    t_pub, age_min, pub_reason = parse_relative_pub_time(ocr_text or "", t_capture_utc=t_capture_utc)

    ocr_metrics = meta.get("ocr_metrics")
    likes = shares = sends = saves = remixes = comments = views = None
    source = "none"

    if isinstance(ocr_metrics, dict):
        likes = _as_int_or_none(ocr_metrics.get("likes"))
        shares = _as_int_or_none(ocr_metrics.get("shares"))
        sends = _as_int_or_none(ocr_metrics.get("sends"))
        saves = _as_int_or_none(ocr_metrics.get("saves"))
        remixes = _as_int_or_none(ocr_metrics.get("remixes"))
        comments = _as_int_or_none(ocr_metrics.get("comments"))
        views = _as_int_or_none(ocr_metrics.get("views"))
        if any(x is not None for x in (likes, shares, sends, saves, remixes, comments, views)):
            source = "ocr_metrics"

    if source == "none":
        likes = _as_int_or_none(meta.get("ocr_likes"))
        shares = _as_int_or_none(meta.get("ocr_shares"))
        sends = _as_int_or_none(meta.get("ocr_sends"))
        saves = _as_int_or_none(meta.get("ocr_saves"))
        remixes = _as_int_or_none(meta.get("ocr_remixes"))
        comments = _as_int_or_none(meta.get("ocr_comments"))
        views = _as_int_or_none(meta.get("ocr_views"))
        if any(x is not None for x in (likes, shares, sends, saves, remixes, comments, views)):
            source = "meta_fields"

    # Last resort (additive): parse counters from the OCR text itself.
    if source == "none" and ocr_text:
        try:
            extracted = extract_metrics_from_ocr_text(ocr_text)
            likes = likes if likes is not None else extracted.get("likes")
            shares = shares if shares is not None else extracted.get("shares")
            sends = sends if sends is not None else extracted.get("sends")
            saves = saves if saves is not None else extracted.get("saves")
            remixes = remixes if remixes is not None else extracted.get("remixes")
            comments = comments if comments is not None else extracted.get("comments")
            views = views if views is not None else extracted.get("views")
            if any(x is not None for x in (likes, shares, sends, saves, remixes, comments, views)):
                source = "ocr_text"
        except Exception:
            pass

    like_v = share_v = send_v = save_v = remix_v = comment_v = stv = None
    notes = ""

    if age_min is None:
        notes = f"âge indisponible ({pub_reason})"
    else:
        if likes is not None and (shares is not None or sends is not None) and comments is not None:
            try:
                like_v, share_v, comment_v, stv, send_v, save_v, remix_v = compute_stv(
                    likes=likes,
                    shares=int(shares if shares is not None else (sends or 0)),
                    comments=comments,
                    age_minutes=age_min,
                    sends=sends,
                    saves=saves,
                    remixes=remixes,
                )
            except Exception:
                notes = "STV non calculable"
        else:
            notes = "métriques OCR incomplètes"

    category = classify(age_minutes=age_min, likes=likes, comments=comments, stv=stv)

    return TemporalAnalysis(
        t_capture_utc=t_capture_utc,
        t_pub_utc=t_pub,
        age_minutes=age_min,
        likes=likes,
        shares=shares,
        sends=sends,
        saves=saves,
        remixes=remixes,
        comments=comments,
        views=views,
        like_velocity=like_v,
        share_velocity=share_v,
        send_velocity=send_v,
        save_velocity=save_v,
        remix_velocity=remix_v,
        comment_velocity=comment_v,
        stv=stv,
        category=category,
        ocr_source=source,
        ocr_pub_raw=ocr_text,
        notes=notes,
    )


def format_telegram_block(a: TemporalAnalysis) -> str:
    """Return a compact Telegram block (3-second scan)."""

    def _fmt_int(n: int | None) -> str:
        if n is None:
            return "N/A"
        return f"{n:,}".replace(",", " ")

    def _fmt_f(x: float | None, nd: int = 1) -> str:
        if x is None:
            return "N/A"
        try:
            return f"{float(x):.{nd}f}"
        except Exception:
            return "N/A"

    if a.age_minutes is None:
        age_human = "indisponible"
    else:
        if a.age_minutes < 120.0:
            age_human = f"il y a {int(round(a.age_minutes))} min"
        else:
            age_human = f"il y a {a.age_minutes / 60.0:.1f} h"

    lines: list[str] = []
    lines.append(f"📅 Publiée : {age_human}")
    parts: list[str] = []
    parts.append(f"❤️ {_fmt_int(a.likes)}")
    parts.append(f"💬 {_fmt_int(a.comments)}")

    if a.sends is not None:
        parts.append(f"✈️ {_fmt_int(a.sends)}")
    if a.saves is not None:
        parts.append(f"🔖 {_fmt_int(a.saves)}")
    if a.remixes is not None:
        parts.append(f"🔁 {_fmt_int(a.remixes)}")

    # Shares is often an alias of sends in our OCR fallback; avoid duplicating.
    if a.shares is not None and (a.sends is None or a.shares != a.sends):
        parts.append(f"📤 {_fmt_int(a.shares)}")

    if a.views is not None:
        parts.append(f"👁️ {_fmt_int(a.views)}")

    lines.append(" | ".join(parts))
    lines.append("")
    lines.append("⚡ Vitesse")
    lines.append(f"❤️ {_fmt_f(a.like_velocity, 1)} likes/min")
    lines.append(f"💬 {_fmt_f(a.comment_velocity, 2)} comm/min")
    lines.append(f"✈️ {_fmt_f(a.send_velocity, 2)} envois/min")
    lines.append(f"🔖 {_fmt_f(a.save_velocity, 2)} enreg/min")
    lines.append(f"🔁 {_fmt_f(a.remix_velocity, 2)} remix/min")
    lines.append(f"📤 {_fmt_f(a.share_velocity, 2)} partages/min")
    lines.append("")
    lines.append(f"🧮 STV : {_fmt_f(a.stv, 2)}")
    lines.append(f"📌 Statut : {a.category}")

    if a.notes:
        lines.append(f"ℹ️ Notes: {a.notes} (source OCR: {a.ocr_source})")

    return "\n".join(lines).strip()
