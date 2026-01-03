from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
import time
import uuid

from .mobile_agent.metrics import SessionMetrics, dict_to_metrics, metrics_to_dict
from .mobile_agent.risk_estimator import RiskAssessment


VideoStatus = Literal["pending", "approved", "rejected", "deleted"]


@dataclass
class VideoItem:
    internal_id: str
    source: str
    source_url: str
    score: float
    status: VideoStatus
    timestamp: float
    session_id: str

    # Selection explainability (V1/V2-like)
    threshold: float = 0.0
    score_details: dict = field(default_factory=dict)
    reason: str = ""

    # Parallel categorization (does NOT change the existing score/threshold).
    score_viral: float = 0.0
    score_latent: float = 0.0
    viral_label: str = ""  # "🔥 VIRAL" | "💎 LATENT" | "❌ IGNORER"

    # V3-only: generated text
    title: str = ""
    hashtags: list[str] = field(default_factory=list)

    # MediaResolver results
    local_media_path: str = ""
    media_status: str = ""  # RESOLVED | RESOLUTION_REQUIRED | FAILED
    media_message: str = ""

    # Telegram message tracking (for edit/delete like V1/V2)
    message_chat_id: int | None = None
    message_id: int | None = None

    # Optional: anything we can observe later (caption snippet, creator, etc.)
    meta: dict = field(default_factory=dict)


@dataclass
class V3State:
    version: int = 1
    active_session_id: str | None = None

    # Persisted runtime settings editable via Telegram.
    # Kept as a plain dict for backwards/forwards compatibility.
    settings: dict = field(default_factory=dict)

    # Human-readable reason for the last automatic stop (optional).
    last_session_stop_reason: str | None = None

    # Pause control for the session loop (Telegram UI)
    session_paused: bool = False

    # Telegram control surface (optional but enables resumable operation).
    control_chat_id: int | None = None

    videos: dict[str, VideoItem] = field(default_factory=dict)

    # Step2: simulation agent session telemetry
    session_metrics: dict[str, SessionMetrics] = field(default_factory=dict)

    # Risk monitoring + Telegram alert throttling
    last_risk: dict | None = None
    last_risk_level: str | None = None
    last_risk_alert_ts: float = 0.0

    # Passive Android readiness
    device_status: str | None = None  # READY | DISCONNECTED | LOCKED
    device_status_ts: float = 0.0
    last_device_alert_ts: float = 0.0

    # Device-visible automation helpers (when enabled)
    last_instagram_launch_ts: float = 0.0
    last_reels_nav_ts: float = 0.0

    # Telegram UI
    last_update_id: int = 0


def new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:10]}"


def new_video_id() -> str:
    return f"vid_{uuid.uuid4().hex[:12]}"


def now_ts() -> float:
    return float(time.time())


def video_to_dict(v: VideoItem) -> dict:
    return {
        "internal_id": v.internal_id,
        "source": v.source,
        "source_url": v.source_url,
        "score": float(v.score),
        "status": v.status,
        "timestamp": float(v.timestamp),
        "session_id": v.session_id,
        "meta": dict(v.meta or {}),
        "threshold": float(getattr(v, "threshold", 0.0) or 0.0),
        "score_details": dict(getattr(v, "score_details", {}) or {}),
        "reason": str(getattr(v, "reason", "") or ""),
        "score_viral": float(getattr(v, "score_viral", 0.0) or 0.0),
        "score_latent": float(getattr(v, "score_latent", 0.0) or 0.0),
        "viral_label": str(getattr(v, "viral_label", "") or ""),
        "title": str(getattr(v, "title", "") or ""),
        "hashtags": list(getattr(v, "hashtags", []) or []),
        "local_media_path": str(getattr(v, "local_media_path", "") or ""),
        "media_status": str(getattr(v, "media_status", "") or ""),
        "media_message": str(getattr(v, "media_message", "") or ""),
        "message_chat_id": getattr(v, "message_chat_id", None),
        "message_id": getattr(v, "message_id", None),
    }


def dict_to_video(d: dict) -> VideoItem:
    return VideoItem(
        internal_id=str(d.get("internal_id") or "").strip(),
        source=str(d.get("source") or "").strip() or "instagram",
        source_url=str(d.get("source_url") or "").strip(),
        score=float(d.get("score") or 0.0),
        status=str(d.get("status") or "pending"),
        timestamp=float(d.get("timestamp") or 0.0),
        session_id=str(d.get("session_id") or "").strip(),
        meta=dict(d.get("meta") or {}),
        threshold=float(d.get("threshold") or 0.0),
        score_details=dict(d.get("score_details") or {}),
        reason=str(d.get("reason") or ""),
        score_viral=float(d.get("score_viral") or 0.0),
        score_latent=float(d.get("score_latent") or 0.0),
        viral_label=str(d.get("viral_label") or ""),
        title=str(d.get("title") or ""),
        hashtags=[str(x) for x in (d.get("hashtags") or []) if str(x).strip()],
        local_media_path=str(d.get("local_media_path") or ""),
        media_status=str(d.get("media_status") or ""),
        media_message=str(d.get("media_message") or ""),
        message_chat_id=(int(d.get("message_chat_id")) if d.get("message_chat_id") is not None else None),
        message_id=(int(d.get("message_id")) if d.get("message_id") is not None else None),
    )


def state_to_dict(st: V3State) -> dict:
    return {
        "version": int(st.version),
        "active_session_id": st.active_session_id,
        "settings": dict(getattr(st, "settings", {}) or {}),
        "last_session_stop_reason": (
            str(getattr(st, "last_session_stop_reason", ""))
            if getattr(st, "last_session_stop_reason", None) is not None
            else None
        ),
        "session_paused": bool(getattr(st, "session_paused", False)),
        "control_chat_id": st.control_chat_id,
        "last_update_id": int(st.last_update_id or 0),
        "session_metrics": {sid: metrics_to_dict(m) for sid, m in (st.session_metrics or {}).items()},
        "last_risk": dict(st.last_risk or {}) if st.last_risk is not None else None,
        "last_risk_level": (str(st.last_risk_level) if st.last_risk_level is not None else None),
        "last_risk_alert_ts": float(st.last_risk_alert_ts or 0.0),
        "device_status": (str(st.device_status) if st.device_status is not None else None),
        "device_status_ts": float(st.device_status_ts or 0.0),
        "last_device_alert_ts": float(st.last_device_alert_ts or 0.0),
        "last_instagram_launch_ts": float(getattr(st, "last_instagram_launch_ts", 0.0) or 0.0),
        "last_reels_nav_ts": float(getattr(st, "last_reels_nav_ts", 0.0) or 0.0),
        "videos": {vid: video_to_dict(v) for vid, v in (st.videos or {}).items()},
    }


def dict_to_state(d: dict) -> V3State:
    st = V3State()
    st.version = int(d.get("version") or 1)
    st.active_session_id = d.get("active_session_id") if d.get("active_session_id") else None

    raw_settings = d.get("settings")
    st.settings = dict(raw_settings) if isinstance(raw_settings, dict) else {}
    st.last_session_stop_reason = (
        str(d.get("last_session_stop_reason"))
        if d.get("last_session_stop_reason") is not None
        else None
    )
    st.session_paused = bool(d.get("session_paused") or False)
    try:
        st.control_chat_id = int(d.get("control_chat_id")) if d.get("control_chat_id") is not None else None
    except Exception:
        st.control_chat_id = None
    st.last_update_id = int(d.get("last_update_id") or 0)

    st.last_risk = d.get("last_risk") if isinstance(d.get("last_risk"), dict) else None
    st.last_risk_level = str(d.get("last_risk_level")) if d.get("last_risk_level") is not None else None
    try:
        st.last_risk_alert_ts = float(d.get("last_risk_alert_ts") or 0.0)
    except Exception:
        st.last_risk_alert_ts = 0.0

    st.device_status = str(d.get("device_status")) if d.get("device_status") is not None else None
    try:
        st.device_status_ts = float(d.get("device_status_ts") or 0.0)
    except Exception:
        st.device_status_ts = 0.0
    try:
        st.last_device_alert_ts = float(d.get("last_device_alert_ts") or 0.0)
    except Exception:
        st.last_device_alert_ts = 0.0

    try:
        st.last_instagram_launch_ts = float(d.get("last_instagram_launch_ts") or 0.0)
    except Exception:
        st.last_instagram_launch_ts = 0.0

    try:
        st.last_reels_nav_ts = float(d.get("last_reels_nav_ts") or 0.0)
    except Exception:
        st.last_reels_nav_ts = 0.0

    raw_metrics = d.get("session_metrics") or {}
    if isinstance(raw_metrics, dict):
        for sid, md in raw_metrics.items():
            if not isinstance(md, dict):
                continue
            try:
                m = dict_to_metrics(md)
                if m.session_id:
                    st.session_metrics[m.session_id] = m
                elif str(sid).strip():
                    m.session_id = str(sid).strip()  # type: ignore[misc]
                    st.session_metrics[m.session_id] = m
            except Exception:
                continue

    vids = d.get("videos") or {}
    if isinstance(vids, dict):
        for vid, vd in vids.items():
            if not isinstance(vd, dict):
                continue
            v = dict_to_video(vd)
            if v.internal_id:
                st.videos[v.internal_id] = v
            elif str(vid).strip():
                v.internal_id = str(vid).strip()
                st.videos[v.internal_id] = v
    return st


def risk_to_dict(r: RiskAssessment) -> dict:
    return {
        "level": r.level,
        "justification": r.justification,
        "remaining_seconds": float(r.remaining_seconds),
    }
