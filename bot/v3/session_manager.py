from __future__ import annotations

from dataclasses import dataclass
import time
import hashlib
import string
import random

from .android_agent import AndroidAgent, AndroidStatus
from .mobile_agent import BaseMobileAgent, RiskAssessment, RiskEstimator, SimulatedMobileAgent
from .mobile_agent.events import BaseEvent
from .mobile_agent.metrics import SessionMetrics
from .selector import Selector
from .state import V3State, VideoItem, new_session_id, new_video_id, now_ts, risk_to_dict
from .storage import save_state_locked
from .text_generator import generate_hashtags, generate_title


@dataclass
class SessionStats:
    seen: int = 0
    kept: int = 0


class SessionManager:
    """Runs an analyze/scroll loop.

    Rule: any selected video is persisted immediately.
    """

    def __init__(
        self,
        *,
        state_file,
        agent: BaseMobileAgent,
        selector: Selector,
        risk_estimator: RiskEstimator,
        android_agent: AndroidAgent | None = None,
    ):
        self._state_file = state_file
        self._selector = selector
        self._agent = agent
        self._risk = risk_estimator
        self._android = android_agent

    def _log_device(self, msg: str) -> None:
        print(f"[DEVICE] {msg}")

    def _settings(self, st: V3State) -> dict:
        s = getattr(st, "settings", {})
        return dict(s) if isinstance(s, dict) else {}

    def _get_bool(self, st: V3State, key: str, default: bool) -> bool:
        s = self._settings(st)
        v = s.get(key, default)
        if isinstance(v, bool):
            return bool(v)
        if isinstance(v, (int, float)):
            return bool(int(v) != 0)
        sv = str(v).strip().lower()
        if sv in {"1", "true", "yes", "on"}:
            return True
        if sv in {"0", "false", "no", "off"}:
            return False
        return bool(default)

    def _get_float(self, st: V3State, key: str, default: float, *, lo: float | None = None, hi: float | None = None) -> float:
        s = self._settings(st)
        try:
            v = float(s.get(key, default))
        except Exception:
            v = float(default)
        if lo is not None:
            v = max(float(lo), v)
        if hi is not None:
            v = min(float(hi), v)
        return float(v)

    def _get_int(self, st: V3State, key: str, default: int, *, lo: int | None = None, hi: int | None = None) -> int:
        s = self._settings(st)
        try:
            v = int(s.get(key, default))
        except Exception:
            v = int(default)
        if lo is not None:
            v = max(int(lo), v)
        if hi is not None:
            v = min(int(hi), v)
        return int(v)

    def _stable_shortcode(self, key: str) -> str:
        # Deterministic base62-ish shortcode from a key.
        h = hashlib.sha256(key.encode("utf-8")).digest()
        alphabet = string.ascii_letters + string.digits
        n = int.from_bytes(h[:8], "big")
        out = []
        for _ in range(11):
            out.append(alphabet[n % len(alphabet)])
            n //= len(alphabet)
        return "".join(out)

    def _stable_igsh(self, key: str) -> str:
        # IG share links often include an igsh query param.
        # We generate a stable, URL-safe token so links look like exportable ones.
        h = hashlib.sha256(("igsh:" + key).encode("utf-8")).digest()
        alphabet = string.ascii_letters + string.digits
        n = int.from_bytes(h[:10], "big")
        out = []
        for _ in range(14):
            out.append(alphabet[n % len(alphabet)])
            n //= len(alphabet)
        return "".join(out)

    def _random_scroll_pause_seconds(self, st: V3State) -> float:
        """Return a randomized pause duration between scrolls.

        We keep existing setting `scroll_pause_seconds` as the base value (so
        your current Settings still work) but apply random jitter every time.

        Optional advanced settings (no UI required):
        - scroll_pause_min_seconds / scroll_pause_max_seconds
        - scroll_pause_jitter_ratio (default 0.35)
        """
        base = self._get_float(st, "scroll_pause_seconds", 0.8, lo=0.05, hi=15.0)

        lo_cfg = self._get_float(st, "scroll_pause_min_seconds", 0.0, lo=0.0, hi=15.0)
        hi_cfg = self._get_float(st, "scroll_pause_max_seconds", 0.0, lo=0.0, hi=15.0)
        if lo_cfg > 0.0 or hi_cfg > 0.0:
            lo = float(lo_cfg if lo_cfg > 0.0 else base)
            hi = float(hi_cfg if hi_cfg > 0.0 else base)
            lo, hi = min(lo, hi), max(lo, hi)
            lo = max(0.05, lo)
            hi = min(15.0, hi)
            if hi <= lo:
                return float(lo)
            return float(lo + (hi - lo) * random.random())

        jitter = self._get_float(st, "scroll_pause_jitter_ratio", 0.35, lo=0.0, hi=0.90)
        lo = max(0.05, base * (1.0 - jitter))
        hi = min(15.0, base * (1.0 + jitter))
        if hi <= lo:
            return float(lo)
        return float(lo + (hi - lo) * random.random())

    def _compute_selection_features(self, events: list[BaseEvent]) -> dict:
        # Build simple criteria (0..1) from events.
        # - rythme: based on pauses (short pauses -> higher rhythm)
        # - banalite: baseline medium
        # - potentiel_viral: boosted if an "open" occurred
        pauses = [getattr(e, "seconds", 0.0) for e in events if getattr(e, "type", "") == "pause"]
        avg_pause = float(sum(pauses) / max(1, len(pauses)))
        # Map avg_pause in [0.5..4.0] roughly to rhythm [0.9..0.2]
        rythme = 0.9 - min(1.0, max(0.0, (avg_pause - 0.5) / 3.5)) * 0.7

        opened = any(getattr(e, "type", "") == "open" for e in events)
        potentiel_viral = 0.55 + (0.25 if opened else 0.0)

        banalite = 0.55
        return {
            "rythme": max(0.0, min(1.0, rythme)),
            "banalite": max(0.0, min(1.0, banalite)),
            "potentiel_viral": max(0.0, min(1.0, potentiel_viral)),
        }

    def _probe_device(self, st: V3State) -> AndroidStatus:
        if not self._android:
            # If no probe configured, assume READY (simulation-only environments).
            status: AndroidStatus = "READY"
        else:
            status = self._android.get_status()
        st.device_status = status
        st.device_status_ts = now_ts()
        return status

    def _ensure_metrics(self, st: V3State, session_id: str) -> SessionMetrics:
        m = (st.session_metrics or {}).get(session_id)
        if m is None:
            now = now_ts()
            m = SessionMetrics(session_id=session_id, started_ts=now, last_event_ts=now)
            st.session_metrics[session_id] = m
        return m

    def _apply_events(self, m: SessionMetrics, events: list[BaseEvent]) -> None:
        for ev in events:
            m.apply_event(ev)

    def start_new_session(self, st: V3State) -> str:
        status = self._probe_device(st)
        if status != "READY":
            # Prevent start if device is not ready.
            st.active_session_id = None
            save_state_locked(self._state_file, st)
            raise RuntimeError(f"Device not ready: {status}")

        sid = new_session_id()
        st.active_session_id = sid
        st.last_session_stop_reason = None
        self._agent.start_session(sid)
        self._ensure_metrics(st, sid)
        save_state_locked(self._state_file, st)
        return sid

    def stop_session(self, st: V3State) -> None:
        try:
            self._agent.stop_session()
        except Exception:
            pass
        st.active_session_id = None
        # Hard stop: prevent any further stepping until explicitly restarted.
        try:
            st.session_paused = True
        except Exception:
            pass
        st.last_session_stop_reason = st.last_session_stop_reason or "stopped_by_user"
        save_state_locked(self._state_file, st)

    def step(
        self,
        st: V3State,
        *,
        auto_scroll: bool = True,
    ) -> tuple[V3State, SessionStats, VideoItem | None, RiskAssessment]:
        stats = SessionStats()

        # If device becomes unavailable/locked mid-session, stop cleanly.
        status = self._probe_device(st)
        if st.active_session_id and status != "READY":
            try:
                self._agent.stop_session()
            except Exception:
                pass
            st.active_session_id = None
            st.last_session_stop_reason = f"device_{status}"
            save_state_locked(self._state_file, st)
            # Risk is not the cause here; return SAFE with device status justification.
            return st, stats, None, RiskAssessment(level="SAFE", justification=f"device_{status}", remaining_seconds=0.0)

        created_session = False
        if not st.active_session_id:
            st.active_session_id = new_session_id()
            self._agent.start_session(str(st.active_session_id))
            st.last_session_stop_reason = None
            created_session = True
            save_state_locked(self._state_file, st)

        sid = str(st.active_session_id)
        self._agent.start_session(sid)
        m = self._ensure_metrics(st, sid)

        events: list[BaseEvent] = []
        opened = False
        pause_s_used: float | None = None
        device_pause_sleep_s: float = 0.0

        if auto_scroll:
            # Optional: make actions visible on the tablet via ADB input.
            # This is opt-in (cfg.allow_input must be True).
            # Persisted toggle: can be flipped from Telegram without restarting.
            if self._android is not None and getattr(self._android, "set_input_enabled", None):
                try:
                    self._android.set_input_enabled(self._get_bool(st, "device_input_enabled", False))
                except Exception:
                    pass

            input_enabled = False
            try:
                input_enabled = bool(
                    self._android is not None
                    and getattr(self._android, "input_enabled", None)
                    and self._android.input_enabled()
                )
            except Exception:
                input_enabled = False

            if self._android is not None and not input_enabled:
                self._log_device("ADB input désactivé (définir V3_ENABLE_DEVICE_INPUT=1)")

            # If Instagram lost focus (ads/external browser), force recovery immediately.
            if self._android is not None and input_enabled:
                try:
                    def _recover_to_reels(*, reason: str) -> bool:
                        self._log_device(f"Recovery Reels: {reason}")

                        # Escalate only when we're clearly off Instagram or when swipe failed.
                        rlow = str(reason).lower()
                        aggressive = ("hors focus" in rlow) or ("post-swipe" in rlow) or ("swipe" in rlow)

                        # Best-effort: close running apps ONLY when Instagram is out of focus.
                        # Otherwise, keep it lightweight to avoid disrupting a correct Reels session.
                        if aggressive:
                            try:
                                if getattr(self._android, "_run_ok", None):
                                    # HOME
                                    self._android._run_ok(["shell", "input", "keyevent", "3"], timeout=4.0)

                                    # Kill background activities (best-effort; may be unsupported on some builds).
                                    ok_kill = self._android._run_ok(["shell", "cmd", "activity", "kill-all"], timeout=6.0)
                                    if not ok_kill:
                                        self._android._run_ok(["shell", "am", "kill-all"], timeout=6.0)

                                    # Ensure Instagram is fully reset.
                                    insta_pkg = str(getattr(getattr(self._android, "_cfg", None), "instagram_package", "") or "com.instagram.android")
                                    if insta_pkg:
                                        self._android._run_ok(["shell", "am", "force-stop", insta_pkg], timeout=6.0)

                                    time.sleep(1.0)
                            except Exception as e:
                                self._log_device(f"ADB close apps échoué: {type(e).__name__}: {e}")

                        try:
                            if getattr(self._android, "launch_instagram", None):
                                # If we're just not on Reels (but Instagram is already foreground),
                                # do not force relaunch; just keep moving.
                                if aggressive:
                                    self._android.launch_instagram()
                                    st.last_instagram_launch_ts = float(time.time())
                                    # Pause to let the UI settle after relaunch.
                                    time.sleep(2.0)
                        except Exception as e:
                            self._log_device(f"ADB relaunch échoué: {type(e).__name__}: {e}")

                        try:
                            if getattr(self._android, "open_reels", None):
                                self._android.open_reels()
                                st.last_reels_nav_ts = float(time.time())
                                time.sleep(1.2)
                        except Exception as e:
                            self._log_device(f"ADB open_reels échoué: {type(e).__name__}: {e}")

                        try:
                            # Prefer a strict check when possible, but don't block processing on flaky UI dumps.
                            if getattr(self._android, "is_probably_on_reels", None):
                                if bool(self._android.is_probably_on_reels()):
                                    return True

                            # Fallback: if Instagram is foreground, we consider recovery good enough
                            # to resume scrolling, even if the Reels heuristic cannot confirm.
                            fg2 = None
                            if getattr(self._android, "get_foreground_package", None):
                                fg2 = self._android.get_foreground_package()
                            insta_pkg2 = str(getattr(getattr(self._android, "_cfg", None), "instagram_package", "") or "com.instagram.android")
                            if fg2 and insta_pkg2 and fg2 == insta_pkg2:
                                return True
                        except Exception:
                            return False
                        return False

                    fg = None
                    if getattr(self._android, "get_foreground_package", None):
                        fg = self._android.get_foreground_package()
                    insta_pkg = str(getattr(getattr(self._android, "_cfg", None), "instagram_package", "") or "com.instagram.android")
                    if fg and insta_pkg and fg != insta_pkg:
                        ok = _recover_to_reels(reason=f"Instagram hors focus (fg={fg})")
                        if not ok:
                            # Older behavior was permissive: keep going even if detection fails.
                            self._log_device("Recovery Reels: échec (continue step)")

                    # If Instagram is foreground but we are not on the Reels viewer, re-open Reels.
                    if (not fg) or (fg == insta_pkg):
                        try:
                            # If UI dumps are failing (no_xml), do NOT force recovery loops.
                            xml_probe = ""
                            try:
                                if getattr(self._android, "_uiautomator_dump_xml_quick", None):
                                    xml_probe = str(self._android._uiautomator_dump_xml_quick(timeout_dump_s=3.0, timeout_cat_s=2.0) or "")
                                elif getattr(self._android, "_uiautomator_dump_xml", None):
                                    xml_probe = str(self._android._uiautomator_dump_xml() or "")
                            except Exception:
                                xml_probe = ""

                            on_reels = False
                            if xml_probe:
                                if getattr(self._android, "is_probably_on_reels", None):
                                    on_reels = bool(self._android.is_probably_on_reels(retries=1, retry_sleep_s=0.0))
                            else:
                                # Unknown state; let the session proceed.
                                on_reels = True

                            if not on_reels:
                                # Cooldown: avoid repeated recovery loops on noisy UI heuristics.
                                now3 = float(time.time())
                                last_reels = float(getattr(st, "last_reels_nav_ts", 0.0) or 0.0)
                                if created_session or last_reels <= 0.0 or (now3 - last_reels) >= 15.0:
                                    ok2 = _recover_to_reels(reason="Pas sur Reels")
                                else:
                                    ok2 = True
                                if not ok2:
                                    # Older behavior was permissive: keep going even if detection fails.
                                    self._log_device("Recovery Reels: toujours pas sur Reels (continue step)")
                        except Exception as e:
                            self._log_device(f"ADB reels check échoué: {type(e).__name__}: {e}")
                except Exception as e:
                    self._log_device(f"ADB focus check échoué: {type(e).__name__}: {e}")

            if self._android is not None and input_enabled:
                try:
                    now = float(time.time())
                    # Pre-flight: at session start we MUST ensure Instagram is foreground
                    # and Reels is opened, otherwise swipes happen on the wrong screen.
                    # After that, keep a cooldown to avoid constant relaunch.
                    last_launch = float(getattr(st, "last_instagram_launch_ts", 0.0) or 0.0)
                    last_reels = float(getattr(st, "last_reels_nav_ts", 0.0) or 0.0)

                    force_prefight = created_session or last_launch <= 0.0 or last_reels <= 0.0

                    # Launch Instagram at most every 30s (or force on session start).
                    if force_prefight or (now - last_launch) >= 30.0:
                        if getattr(self._android, "launch_instagram", None):
                            ok = self._android.launch_instagram()
                            if ok:
                                st.last_instagram_launch_ts = now
                                # Give the UI a moment to settle so the next intent works.
                                try:
                                    time.sleep(1.0)
                                except Exception:
                                    pass

                    # Navigate to Reels at most every 20s (or force on session start).
                    if force_prefight or (now - last_reels) >= 20.0:
                        if getattr(self._android, "open_reels", None):
                            ok2 = self._android.open_reels()
                            if ok2:
                                st.last_reels_nav_ts = now
                                try:
                                    time.sleep(1.0)
                                except Exception:
                                    pass
                except Exception as e:
                    self._log_device(f"ADB preflight échoué: {type(e).__name__}: {e}")

            did_swipe = False
            if self._android is not None and input_enabled:
                # SOFT SAFETY GATE (requested): do ONE check.
                # - If UI dump is unavailable (no_xml/timeouts), do NOT block swiping.
                # - If we DO have XML evidence and it says "not on Reels", try open_reels once.
                xml_probe = ""
                try:
                    if getattr(self._android, "_uiautomator_dump_xml_quick", None):
                        xml_probe = str(self._android._uiautomator_dump_xml_quick(timeout_dump_s=3.0, timeout_cat_s=2.0) or "")
                    elif getattr(self._android, "_uiautomator_dump_xml", None):
                        xml_probe = str(self._android._uiautomator_dump_xml() or "")
                except Exception:
                    xml_probe = ""

                if not xml_probe:
                    self._log_device("SAFETY: reels check indisponible (no_xml) → swipe autorisé + vérif post-swipe")
                else:
                    on_reels_now = False
                    try:
                        if getattr(self._android, "is_probably_on_reels", None):
                            on_reels_now = bool(self._android.is_probably_on_reels(retries=1, retry_sleep_s=0.0))
                    except Exception:
                        on_reels_now = False

                    if not on_reels_now:
                        self._log_device("SAFETY: pas sur Reels (XML OK) avant swipe → tentative open_reels")
                        try:
                            if getattr(self._android, "open_reels", None):
                                self._android.open_reels()
                                st.last_reels_nav_ts = float(time.time())
                                time.sleep(1.0)
                        except Exception as e:
                            self._log_device(f"SAFETY: open_reels échoué: {type(e).__name__}: {e}")

                        try:
                            if getattr(self._android, "is_probably_on_reels", None):
                                on_reels_now = bool(self._android.is_probably_on_reels(retries=1, retry_sleep_s=0.0))
                        except Exception:
                            on_reels_now = False

                    if not on_reels_now:
                        # Here we have XML evidence we're not on Reels; recover instead of swiping blindly.
                        self._log_device("SAFETY: toujours pas sur Reels (XML OK) → recovery")
                        try:
                            # Reuse the earlier recovery logic by triggering it through focus mismatch path.
                            # Lightweight: just call open_reels; aggressive relaunch happens post-swipe if needed.
                            if getattr(self._android, "open_reels", None):
                                self._android.open_reels()
                                st.last_reels_nav_ts = float(time.time())
                                time.sleep(1.0)
                        except Exception:
                            pass

            self._log_device("SCROLL attendu sur le device")
            if self._android is not None and input_enabled:
                try:
                    if getattr(self._android, "swipe_up", None):
                        did_swipe = bool(self._android.swipe_up())
                    else:
                        did_swipe = False
                except Exception as e:
                    self._log_device(f"ADB swipe échoué: {type(e).__name__}: {e}")
                    did_swipe = False
            events.append(self._agent.scroll())

            # Post-swipe verification (requested): if swipe failed OR Instagram is no longer foreground,
            # force-stop + relaunch + open Reels to continue.
            if self._android is not None and input_enabled:
                try:
                    insta_pkg = str(getattr(getattr(self._android, "_cfg", None), "instagram_package", "") or "com.instagram.android")
                    fg_post = None
                    if getattr(self._android, "get_foreground_package", None):
                        fg_post = self._android.get_foreground_package()
                    if (not did_swipe) or (fg_post and insta_pkg and fg_post != insta_pkg):
                        reason = "post-swipe: swipe_ko" if (not did_swipe) else f"post-swipe: Instagram hors focus (fg={fg_post})"
                        self._log_device(f"Recovery Reels: {reason}")

                        # Aggressive reset as requested.
                        try:
                            if getattr(self._android, "_run_ok", None):
                                self._android._run_ok(["shell", "input", "keyevent", "3"], timeout=4.0)
                                self._android._run_ok(["shell", "am", "force-stop", insta_pkg], timeout=6.0)
                                time.sleep(1.0)
                        except Exception as e:
                            self._log_device(f"ADB force-stop échoué: {type(e).__name__}: {e}")

                        try:
                            if getattr(self._android, "launch_instagram", None):
                                self._android.launch_instagram()
                                st.last_instagram_launch_ts = float(time.time())
                                time.sleep(2.0)
                        except Exception as e:
                            self._log_device(f"ADB relaunch échoué: {type(e).__name__}: {e}")

                        try:
                            if getattr(self._android, "open_reels", None):
                                self._android.open_reels()
                                st.last_reels_nav_ts = float(time.time())
                                time.sleep(1.2)
                        except Exception as e:
                            self._log_device(f"ADB open_reels échoué: {type(e).__name__}: {e}")

                        stats.seen += 1
                        self._apply_events(m, events)
                        return st, stats, None, RiskAssessment(level="SAFE", justification="recovered_post_swipe", remaining_seconds=0.0)
                except Exception as e:
                    self._log_device(f"Post-swipe check échoué: {type(e).__name__}: {e}")

            # Post-swipe ad check: detect sponsored/ad on the newly visible Reel.
            # If it's an ad, swipe again immediately and skip any further processing.
            if self._android is not None and input_enabled and did_swipe and getattr(self._android, "is_probably_ad_reel", None):
                self._log_device("ANALYSE PUB (post-swipe): démarrage")
                is_ad = False
                try:
                    is_ad = bool(self._android.is_probably_ad_reel())
                except Exception:
                    is_ad = False
                self._log_device(f"ANALYSE PUB (post-swipe): {'OUI' if is_ad else 'NON'}")
                if is_ad:
                    self._log_device("PUB détectée (post-swipe) → swipe immédiat, pas de pause, pas d'extraction")
                    try:
                        if getattr(self._android, "swipe_up", None):
                            self._android.swipe_up()
                    except Exception as e:
                        self._log_device(f"ADB swipe pub échoué: {type(e).__name__}: {e}")
                    stats.seen += 1
                    self._apply_events(m, events)
                    return st, stats, None, RiskAssessment(level="SAFE", justification="ad_skip", remaining_seconds=0.0)

            # No real waiting; we only record a pause event.
            pause_s = self._random_scroll_pause_seconds(st)
            pause_s_used = float(pause_s)
            self._log_device(f"PAUSE attendue sur le device ({pause_s_used:.2f}s)")
            events.append(self._agent.pause(pause_s_used))
            # IMPORTANT: do not block BEFORE URL extraction.
            # If we want a visible pause on the device, we'll sleep AFTER the URL capture attempt.
            if self._android is not None and (did_swipe or input_enabled):
                try:
                    device_pause_sleep_s = float(pause_s_used or 0.0)
                except Exception:
                    device_pause_sleep_s = 0.0

            # Optional open + watch pause (simulation only)
            if isinstance(self._agent, SimulatedMobileAgent) and self._agent.should_open_after_scroll():
                self._log_device("OPEN vidéo attendu sur le device")
                if self._android is not None and input_enabled:
                    try:
                        if getattr(self._android, "tap_center", None):
                            did_tap = bool(self._android.tap_center())
                        else:
                            did_tap = False
                    except Exception as e:
                        self._log_device(f"ADB tap échoué: {type(e).__name__}: {e}")
                        did_tap = False
                events.append(self._agent.open())
                opened = True
                ow_min = self._get_float(st, "open_watch_min_seconds", 1.5, lo=0.1, hi=60.0)
                ow_max = self._get_float(st, "open_watch_max_seconds", 4.0, lo=0.1, hi=60.0)
                lo_w = min(ow_min, ow_max)
                hi_w = max(ow_min, ow_max)
                watch_s = lo_w + (hi_w - lo_w) * random.random()
                self._log_device(f"VISIONNAGE attendu sur le device ({watch_s:.1f}s)")
                events.append(self._agent.pause(watch_s))
                # IMPORTANT: do NOT block reel watching with a real sleep.
                # We still record the pause event for scoring/telemetry.

        stats.seen += 1

        self._apply_events(m, events)

        # Create a synthetic "observation" for the selector.
        # Keep logic consistent with existing Selector heuristics.
        class _Obs:
            source = "simulated"
            source_url = ""
            meta = ({"opened": True} if opened else {})
            observed_at = float(time.time())

        features = self._compute_selection_features(events)
        _Obs.meta = {**(_Obs.meta or {}), **features}

        decision = self._selector.decide(_Obs(), settings=self._settings(st))  # type: ignore[arg-type]
        # Try to derive a stable identity so we can recognize the same video again.
        content_key = None
        for e in reversed(events):
            try:
                meta = getattr(e, "meta", None)
                if isinstance(meta, dict) and meta.get("content_key"):
                    content_key = str(meta.get("content_key") or "").strip()
                    if content_key:
                        break
            except Exception:
                pass

        identity_key = content_key or new_video_id()

        # Nouvelle logique : n'extraire l'URL que si la vidéo est retenue
        source_url = ""
        clipboard_url = ""
        extracted_shortcode = ""
        keep_requested = bool(getattr(decision, "should_keep", False))
        if keep_requested:
            if self._android is None or not getattr(self._android, "copy_current_reel_link_from_share_sheet", None):
                self._log_device("URL copy skip: AndroidAgent non disponible")
            else:
                try:
                    source_url = self._android.copy_current_reel_link_from_share_sheet() or ""
                except Exception as e:
                    self._log_device(f"URL copy failed: {type(e).__name__}: {e}")
                    source_url = ""
        clipboard_url = str(source_url or "")
        keep_ok = bool(keep_requested and clipboard_url)
        if keep_requested and not clipboard_url:
            self._log_device("URL capture échouée (keep=True) → skip et on passe au suivant")
        elif keep_ok:
            try:
                u = str(clipboard_url or "").strip()
                if len(u) > 180:
                    u = u[:177] + "..."
                self._log_device(f"URL capturée OK: {u}")
            except Exception:
                self._log_device("URL capturée OK")

        # Visible pause on the device (deferred until AFTER URL capture attempt).
        if device_pause_sleep_s and device_pause_sleep_s > 0.0:
            try:
                time.sleep(float(device_pause_sleep_s))
            except Exception:
                pass
        # On extrait le shortcode uniquement pour l'identifiant interne, mais on ne reconstitue plus l'URL
        import re
        shortcode = ""
        reel_match = re.search(r"/reel/([A-Za-z0-9_-]{5,})", source_url)
        if reel_match:
            shortcode = str(reel_match.group(1)).strip()
        extracted_shortcode = str(shortcode or "")
        if not shortcode:
            shortcode = self._stable_shortcode(identity_key)
        vid = f"vid_{shortcode}"

        # If we've already seen this video, update last-seen metadata and don't create a new entry.
        existing = st.videos.get(vid)
        if existing is not None:
            existing.timestamp = now_ts()
            try:
                existing.meta = dict(getattr(existing, "meta", {}) or {})
                existing.meta["last_seen_ts"] = float(time.time())
                existing.meta["seen_count"] = int(existing.meta.get("seen_count") or 0) + 1
                existing.meta["last_seen_session_id"] = str(sid)
            except Exception:
                pass

            # Refresh scoring fields for visibility (but do not override moderation decisions).
            try:
                existing.score = float(getattr(decision, "score", 0.0) or 0.0)
                existing.threshold = float(getattr(decision, "threshold", 0.0) or 0.0)
                existing.score_details = dict(getattr(decision, "details", {}) or {})
                existing.reason = str(getattr(decision, "reason", "") or "")
                existing.score_viral = float(getattr(decision, "score_viral", 0.0) or 0.0)
                existing.score_latent = float(getattr(decision, "score_latent", 0.0) or 0.0)
                existing.viral_label = str(getattr(decision, "viral_label", "") or "")
                existing.source_url = str(source_url or existing.source_url)
                # Regenerate text to reflect the current scoring.
                existing.title = generate_title(score=existing.score, reason=existing.reason, source_url=existing.source_url)
                existing.hashtags = generate_hashtags(score_details=existing.score_details)
            except Exception:
                pass

            promoted = False
            # If it was previously deleted and now scores as keep, bring it back to pending.
            # Never downgrade pending/approved/rejected here.
            try:
                if str(getattr(existing, "status", "")) == "deleted" and keep_ok:
                    existing.status = "pending"
                    promoted = True
            except Exception:
                promoted = False

            # Persist immediately.
            st.videos[vid] = existing
            risk = self._risk.assess(m, now_ts=now_ts())
            st.last_risk = risk_to_dict(risk)
            st.last_risk_level = risk.level
            save_state_locked(self._state_file, st)
            if promoted:
                stats.kept += 1
                return st, stats, existing, risk
            # If not promoted, keep existing behavior: only return an item when it's pending.
            if str(getattr(existing, "status", "")) == "pending":
                return st, stats, existing, risk
            return st, stats, None, risk

        item = VideoItem(
            internal_id=vid,
            source="instagram",
            source_url=source_url,
            score=float(decision.score),
            status=("pending" if keep_ok else "deleted"),
            timestamp=now_ts(),
            session_id=sid,
            threshold=float(getattr(decision, "threshold", 0.0) or 0.0),
            score_details=dict(getattr(decision, "details", {}) or {}),
            reason=str(getattr(decision, "reason", "") or ""),
            score_viral=float(getattr(decision, "score_viral", 0.0) or 0.0),
            score_latent=float(getattr(decision, "score_latent", 0.0) or 0.0),
            viral_label=str(getattr(decision, "viral_label", "") or ""),
            meta={
                "events": [e.type for e in events],
                "device_actions": [
                    "SCROLL attendu sur le device",
                    f"PAUSE attendue sur le device ({(pause_s_used if pause_s_used is not None else 0.8):.2f}s)",
                    *( ["OPEN vidéo attendu sur le device"] if opened else [] ),
                ],
                "extracted_shortcode": str(extracted_shortcode or ""),
                "clipboard_url": str(clipboard_url or ""),
                "selector_reason": decision.reason,
                "viral_label": str(getattr(decision, "viral_label", "") or ""),
                "score_viral": float(getattr(decision, "score_viral", 0.0) or 0.0),
                "score_latent": float(getattr(decision, "score_latent", 0.0) or 0.0),
                "observed_at": float(time.time()),
                "seen_count": 1,
                "last_seen_ts": float(time.time()),
                "content_key": str(content_key or ""),
            },
        )

        item.title = generate_title(score=item.score, reason=item.reason, source_url=item.source_url)
        item.hashtags = generate_hashtags(score_details=item.score_details)

        # Persist immediately.
        st.videos[item.internal_id] = item

        risk = self._risk.assess(m, now_ts=now_ts())
        st.last_risk = risk_to_dict(risk)
        st.last_risk_level = risk.level

        # Auto-stop session if risk becomes HIGH_RISK.
        if risk.level == "HIGH_RISK" and self._get_bool(st, "risk_safety_enabled", True):
            try:
                self._agent.stop_session()
            except Exception:
                pass
            st.active_session_id = None
            st.last_session_stop_reason = "high_risk"
        elif risk.level == "HIGH_RISK":
            self._log_device("Risk HIGH_RISK détecté mais sécurité désactivée (continue)")

        save_state_locked(self._state_file, st)
        if keep_ok:
            stats.kept += 1
            return st, stats, item, risk
        return st, stats, None, risk
