from __future__ import annotations

from dataclasses import dataclass

from ..android_agent import AndroidAgent
from ..mobile_agent import BaseMobileAgent, RiskAssessment, RiskEstimator
from ..selector import Selector
from ..session_manager import SessionManager
from ..state import V3State
from ..storage import load_state_locked, save_state_locked


@dataclass(frozen=True)
class HandlerDeps:
    state_file: object
    agent: BaseMobileAgent
    android_agent: AndroidAgent | None
    selector: Selector
    risk_estimator: RiskEstimator


def is_allowed(chat_id: int, *, allowed: set[int]) -> bool:
    return (not allowed) or (int(chat_id) in allowed)


def start_session(deps: HandlerDeps, st: V3State, chat_id: int) -> V3State:
    sm = SessionManager(
        state_file=deps.state_file,
        agent=deps.agent,
        selector=deps.selector,
        risk_estimator=deps.risk_estimator,
        android_agent=deps.android_agent,
    )
    st.control_chat_id = int(chat_id)
    try:
        sm.start_new_session(st)
    except Exception:
        # start_new_session already persisted device_status; leave session stopped.
        save_state_locked(deps.state_file, st)
    return st


def stop_session(deps: HandlerDeps, st: V3State) -> V3State:
    sm = SessionManager(
        state_file=deps.state_file,
        agent=deps.agent,
        selector=deps.selector,
        risk_estimator=deps.risk_estimator,
        android_agent=deps.android_agent,
    )
    sm.stop_session(st)
    return st


def step_once(deps: HandlerDeps) -> tuple[V3State, object | None, RiskAssessment]:
    st = load_state_locked(deps.state_file)
    sm = SessionManager(
        state_file=deps.state_file,
        agent=deps.agent,
        selector=deps.selector,
        risk_estimator=deps.risk_estimator,
        android_agent=deps.android_agent,
    )
    st, _, item, risk = sm.step(st, auto_scroll=True)
    save_state_locked(deps.state_file, st)
    return st, item, risk


def set_status(deps: HandlerDeps, vid: str, status: str) -> V3State:
    st = load_state_locked(deps.state_file)
    v = st.videos.get(vid)
    if not v:
        return st
    v.status = status  # type: ignore[assignment]
    save_state_locked(deps.state_file, st)
    return st


def like_if_approved(deps: HandlerDeps, vid: str) -> tuple[V3State, bool]:
    st = load_state_locked(deps.state_file)
    v = st.videos.get(vid)
    if not v or v.status != "approved":
        return st, False

    # Step2: simulated-only. We do not automate Instagram actions.
    v.meta = dict(v.meta or {})
    v.meta["liked_simulated"] = True
    v.meta["liked_at"] = v.meta.get("liked_at") or __import__("time").time()
    save_state_locked(deps.state_file, st)
    return st, True
