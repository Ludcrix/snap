from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
import html
import os
import re
import subprocess
import time
import unicodedata


AndroidStatus = Literal["READY", "DISCONNECTED", "LOCKED"]


@dataclass(frozen=True)
class AndroidAgentConfig:
    adb_path: str = "adb"
    serial: str | None = None

    # Opt-in: enable ADB input events (tap/swipe) so actions are visible.
    allow_input: bool = False
    instagram_package: str = "com.instagram.android"
    swipe_duration_ms: int = 420
    swipe_margin_ratio: float = 0.18
    tap_to_open: bool = True


class AndroidAgent:
    """Android device probe, with optional device-visible actions.

    Default (safe) mode:
    - Read-only ADB commands only (status probe).

    Optional mode (opt-in):
    - If cfg.allow_input=True, enables ADB input events (tap/swipe) to make
      the V3 session actions visible on the tablet screen.

    Exposes status: READY | DISCONNECTED | LOCKED
    """

    def __init__(self, cfg: AndroidAgentConfig):
        self._cfg = cfg
        # Allow runtime toggling of visible input (tap/swipe) without rebuilding deps.
        self._allow_input: bool = bool(getattr(cfg, "allow_input", False))
        # UIAutomator dumps can be flaky/expensive; cache briefly.
        self._last_ui_dump_ts: float = 0.0
        self._last_ui_dump_xml: str | None = None
        self._last_ui_dump_source: str = ""
        # Track last positive Reels detection to handle transient dump failures.
        self._last_reels_yes_ts: float = 0.0
        # Throttle debug logs for Reels detection.
        self._last_reels_dbg_ts: float = 0.0
        # Track when we last attempted to navigate to Reels.
        # Used to make dumpsys-based fallbacks safe (bounded in time).
        self._last_open_reels_ts: float = 0.0

    def _uiautomator_dump_xml_quick(self, *, timeout_dump_s: float = 3.5, timeout_cat_s: float = 2.5) -> str:
        """Fast, resilient UIAutomator dump.

        On some devices, `exec-out uiautomator dump /dev/tty` hangs and times out.
        The file-based dump is often more reliable, so we try it first with short timeouts.
        """
        # 1) Prefer file-based dump (often more reliable on some ROMs).
        try:
            ok = self._run_ok(
                ["shell", "uiautomator", "dump", "--compressed", "/sdcard/window_dump.xml"],
                timeout=float(timeout_dump_s),
            )
            if ok:
                out = self._run(["shell", "cat", "/sdcard/window_dump.xml"], timeout=float(timeout_cat_s)) or ""
                if out and ("<?xml" in out or "<hierarchy" in out):
                    try:
                        self._last_ui_dump_ts = float(time.time())
                        self._last_ui_dump_xml = out
                        self._last_ui_dump_source = "file"
                    except Exception:
                        pass
                    return out
        except Exception:
            pass

        # 2) Fallback: stdout dump.
        try:
            out = self._run(["exec-out", "uiautomator", "dump", "/dev/tty"], timeout=float(timeout_dump_s)) or ""
            if out and ("<?xml" in out or "<hierarchy" in out):
                try:
                    self._last_ui_dump_ts = float(time.time())
                    self._last_ui_dump_xml = out
                    self._last_ui_dump_source = "exec-out"
                except Exception:
                    pass
                return out
        except Exception:
            pass

        try:
            self._last_ui_dump_source = ""
        except Exception:
            pass
        return ""

    def is_probably_ad_reel(self) -> bool:
        """Heuristic: return True if the currently visible Reel looks sponsored/advertising.

        IMPORTANT: This is intentionally a *post-swipe* check. SessionManager calls it
        right after a swipe to avoid processing ads.
        """
        debug_ads = (
            str(os.getenv("V3_ANDROID_DEBUG", "")).strip() == "1"
            or str(os.getenv("V3_DEBUG_ANDROID", "")).strip() == "1"
        )

        def _dbg(msg: str) -> None:
            if debug_ads:
                print(f"[DEVICE] {msg}", flush=True)

        def _ctx(s: str, token: str, *, span: int = 60) -> str:
            try:
                i = s.find(token)
                if i < 0:
                    return ""
                a = max(0, i - span)
                b = min(len(s), i + len(token) + span)
                return s[a:b].replace("\n", " ")
            except Exception:
                return ""

        def _norm_no_accents(s: str) -> str:
            s = (s or "").lower()
            s = unicodedata.normalize("NFKD", s)
            return "".join(ch for ch in s if not unicodedata.combining(ch))

        def _letters_digits_spaces(s: str) -> str:
            # Helps match mojibake like "Sponsoris├®" -> "sponsoris"
            out: list[str] = []
            for ch in (s or ""):
                if ch.isalnum() or ch.isspace():
                    out.append(ch)
            return "".join(out)

        # Keep these mostly ASCII; we normalize the XML to remove accents.
        # Include a very loose "sponsoris" token to survive mojibake/console codepages.
        ad_markers = [
            "sponsored",
            "sponsorise",
            "sponsoris",
            "commandite",
            "publicite",
            "paid partnership",
            "partenariat remunere",
            "en partenariat avec",
            "promoted",
            "promotion",
            "adchoices",
            # Common ad CTAs (sometimes present in UIAutomator hierarchy)
            "en savoir plus",
            "learn more",
            "shop now",
            "acheter",
        ]

        def _check_once() -> tuple[bool, str, int]:
            try:
                # IMPORTANT: keep this fast and non-blocking.
                # Use the quick dump helper to avoid exec-out hangs.
                xml = self._uiautomator_dump_xml_quick(timeout_dump_s=3.0, timeout_cat_s=2.0) or ""
            except Exception:
                xml = ""
            low_raw = (xml or "").lower()
            if not low_raw:
                return False, "no_xml", 0

            # Normalize to improve matching across locales and encoding glitches.
            low = _norm_no_accents(low_raw)
            low_loose = _letters_digits_spaces(low)

            if debug_ads:
                src = getattr(self, "_last_ui_dump_source", "")
                has_sponsor = ("sponsor" in low_loose) or ("sponsoris" in low_loose)
                _dbg(
                    f"ad_dump dump={src!r} xml_len={len(xml)} has_sponsor={has_sponsor} "
                    f"has_publicit={'publicit' in low_loose} has_promoted={'promoted' in low_loose}"
                )

            hit = next((m for m in ad_markers if (m in low) or (m in low_loose)), "")
            if hit:
                if debug_ads:
                    src = getattr(self, "_last_ui_dump_source", "")
                    _dbg(
                        f"ad_marker_hit marker={hit!r} dump={src!r} xml_len={len(xml)} "
                        f"ctx={_ctx(low_loose, hit) or _ctx(low, hit)!r}"
                    )
                return True, hit, len(low)

            # Fallback regex: markers may appear in text/content-desc.
            try:
                ok = bool(
                    re.search(
                        r"(text|content-desc)=\"[^\"]*(sponsor|sponsored|sponsorise|sponsoris|commandit|publicit|adchoices|paid partnership|partenariat|promoted|promotion|en savoir plus|learn more|shop now|acheter)[^\"]*\"",
                        low,
                    )
                    or re.search(
                        r"(text|content-desc)=\"[^\"]*(sponsor|sponsored|sponsorise|sponsoris|commandit|publicit|adchoices|paid partnership|partenariat|promoted|promotion|en savoir plus|learn more|shop now|acheter)[^\"]*\"",
                        low_loose,
                    )
                )
            except Exception:
                ok = False
            if ok and debug_ads:
                src = getattr(self, "_last_ui_dump_source", "")
                # Provide a small hint about what's inside when regex hits.
                hint = ""
                for t in ("sponsor", "sponsoris", "sponsorise", "publicite", "promoted", "adchoices", "paid partnership"):
                    if t in low_loose:
                        hint = t
                        break
                _dbg(
                    f"ad_regex_hit hint={hint!r} dump={src!r} xml_len={len(xml)} "
                    f"ctx={_ctx(low_loose, hint) if hint else ''!r}"
                )

            if (not ok) and debug_ads:
                # If sponsor-ish content is present but we still return no_hit, log a short context.
                if ("sponsor" in low_loose) or ("sponsoris" in low_loose):
                    token = "sponsor" if "sponsor" in low_loose else "sponsoris"
                    _dbg(
                        f"ad_no_hit_but_sponsor_present token={token!r} ctx={_ctx(low_loose, token)!r}"
                    )
            return bool(ok), ("regex" if ok else "no_hit"), len(low)

        try:
            # Two-pass confirmation to avoid false positives from the previous reel
            # during UI transition right after a swipe.
            hit1, why1, n1 = _check_once()
            try:
                time.sleep(0.25)
            except Exception:
                pass
            hit2, why2, n2 = _check_once()

            detected = bool(hit1 and hit2)

            # Degraded confirmation: if dumps are flaky and one pass is missing (no_xml)
            # but the other pass has a strong marker hit (not just regex), accept it.
            strong1 = bool(hit1 and why1 not in ("regex", "no_xml", "no_hit"))
            strong2 = bool(hit2 and why2 not in ("regex", "no_xml", "no_hit"))
            if not detected:
                if strong1 and (why2 == "no_xml"):
                    detected = True
                    if debug_ads:
                        _dbg("ad_check degraded_confirm=hit1_strong_hit2_no_xml")
                elif strong2 and (why1 == "no_xml"):
                    detected = True
                    if debug_ads:
                        _dbg("ad_check degraded_confirm=hit2_strong_hit1_no_xml")

            if debug_ads:
                _dbg(
                    f"ad_check hit1={hit1} why1={why1!r} len1={n1} | "
                    f"hit2={hit2} why2={why2!r} len2={n2} -> detected={detected}"
                )

            return detected
        except Exception:
            return False

    def adb_available(self) -> bool:
        """Return True if the configured adb binary can be executed."""
        try:
            p = subprocess.run(
                [self._cfg.adb_path, "version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2.5,
            )
            return p.returncode == 0
        except FileNotFoundError:
            return False
        except Exception:
            return False

    def input_enabled(self) -> bool:
        return bool(self._allow_input)

    def set_input_enabled(self, enabled: bool) -> None:
        self._allow_input = bool(enabled)

    def _adb_base(self) -> list[str]:
        cmd = [self._cfg.adb_path]
        if self._cfg.serial:
            cmd += ["-s", self._cfg.serial]
        return cmd

    def _run(self, args: list[str], *, timeout: float = 8.0) -> str | None:
        cmd = self._adb_base() + args
        debug = (
            str(os.getenv("V3_ANDROID_DEBUG", "")).strip() == "1"
            or str(os.getenv("V3_DEBUG_ANDROID", "")).strip() == "1"
        )
        try:
            p = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except FileNotFoundError:
            if debug:
                print(f"[android][adb] not_found cmd={cmd}", flush=True)
            return None
        except Exception as e:
            if debug:
                print(f"[android][adb] exception={type(e).__name__} cmd={cmd}", flush=True)
            return None

        if p.returncode != 0:
            if debug:
                err = (p.stderr or "").strip()
                out = (p.stdout or "").strip()
                # Keep logs bounded.
                if len(err) > 500:
                    err = err[:500] + "..."
                if len(out) > 500:
                    out = out[:500] + "..."
                print(f"[android][adb] failed rc={p.returncode} cmd={cmd} stderr={err!r} stdout={out!r}", flush=True)
            return None
        return (p.stdout or "").strip()

    def _run_ok(self, args: list[str], *, timeout: float = 8.0) -> bool:
        cmd = self._adb_base() + args
        debug = (
            str(os.getenv("V3_ANDROID_DEBUG", "")).strip() == "1"
            or str(os.getenv("V3_DEBUG_ANDROID", "")).strip() == "1"
        )
        try:
            p = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except Exception:
            if debug:
                print(f"[android][adb] exception cmd={cmd}", flush=True)
            return False
        if p.returncode != 0 and debug:
            err = (p.stderr or "").strip()
            out = (p.stdout or "").strip()
            if len(err) > 500:
                err = err[:500] + "..."
            if len(out) > 500:
                out = out[:500] + "..."
            print(f"[android][adb] failed rc={p.returncode} cmd={cmd} stderr={err!r} stdout={out!r}", flush=True)
        return p.returncode == 0

    def press_back(self) -> bool:
        """Press Android BACK key once (visible navigation)."""
        self._require_input_allowed()
        return self._run_ok(["shell", "input", "keyevent", "4"], timeout=4.0)

    def _tap_bounds(self, bounds: str) -> bool:
        """Tap the center of UIAutomator bounds like: [x1,y1][x2,y2]."""
        self._require_input_allowed()
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", str(bounds or "").strip())
        if not m:
            return False
        x1, y1, x2, y2 = (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
        cx = int((x1 + x2) / 2)
        cy = int((y1 + y2) / 2)
        return self._run_ok(["shell", "input", "tap", str(cx), str(cy)], timeout=4.0)

    def _find_bounds_for_any_resource_id_fragment(self, xml: str, fragments: list[str]) -> str | None:
        """Return bounds for first node whose resource-id contains any fragment."""
        if not xml:
            return None
        cleaned = [str(f or "").strip() for f in (fragments or []) if str(f or "").strip()]
        if not cleaned:
            return None

        for m in re.finditer(r"<node\b[^>]*>", xml):
            tag = m.group(0)
            rid_m = re.search(r"\bresource-id=\"([^\"]*)\"", tag)
            rid = str(rid_m.group(1) if rid_m else "")
            if not rid:
                continue
            bounds_m = re.search(r"\bbounds=\"([^\"]+)\"", tag)
            bounds = str(bounds_m.group(1) if bounds_m else "").strip()
            if not bounds:
                continue
            for frag in cleaned:
                if frag in rid:
                    return bounds
        return None

    def _find_bounds_for_any_label(self, xml: str, labels: list[str]) -> str | None:
        """Return bounds for first node matching any label in text/content-desc.

        Tries exact match first, then a case-insensitive "contains" match.
        """
        if not xml:
            return None
        cleaned = [str(lab or "").strip() for lab in (labels or []) if str(lab or "").strip()]
        if not cleaned:
            return None

        # UIAutomator does not guarantee attribute order in <node ...>.
        # Parse each node tag and match against text/content-desc.
        node_tags = re.finditer(r"<node\b[^>]*>", xml)

        def _attr(tag: str, name: str) -> str:
            m = re.search(rf"\b{name}=\"([^\"]*)\"", tag)
            return str(m.group(1) if m else "")

        # Exact match pass.
        for m in node_tags:
            tag = m.group(0)
            bounds = _attr(tag, "bounds").strip()
            if not bounds:
                continue
            text = _attr(tag, "text").strip()
            desc = _attr(tag, "content-desc").strip()
            for lab in cleaned:
                if text == lab or desc == lab:
                    return bounds

        # Contains match pass (case-insensitive).
        node_tags = re.finditer(r"<node\b[^>]*>", xml)
        for m in node_tags:
            tag = m.group(0)
            bounds = _attr(tag, "bounds").strip()
            if not bounds:
                continue
            text = _attr(tag, "text").strip()
            desc = _attr(tag, "content-desc").strip()
            hay = f"{text} {desc}".lower()
            if not hay.strip():
                continue
            for lab in cleaned:
                if lab.lower() in hay:
                    return bounds
        return None

    def copy_current_reel_link_from_share_sheet(self) -> str | None:
        """Open the Instagram share sheet and extract the Reel URL from UIAutomator dump.

        This intentionally avoids Android clipboard access (some devices do not implement
        `cmd clipboard get` for shell).

        Minimal implementation: only executed when input is enabled.
        Best-effort; returns None if the UI elements are not found.
        """
        self._require_input_allowed()
        debug = (
            str(os.getenv("V3_ANDROID_DEBUG", "")).strip() == "1"
            or str(os.getenv("V3_DEBUG_ANDROID", "")).strip() == "1"
        )

        def _dbg(msg: str) -> None:
            if debug:
                print(f"[android][share_sheet] {msg}", flush=True)

        def _dbg_xml(name: str, xml: str) -> None:
            if not debug:
                return
            s = xml or ""
            _dbg(
                f"{name}: len={len(s)} http={s.count('http')} https={s.count('https')} "
                f"instagram={s.lower().count('instagram')} reel_slash={s.lower().count('/reel/')} "
                f"reels_slash={s.lower().count('/reels/')}" 
            )
            # Show limited context around first occurrences of "instagram" and "reel".
            low = s.lower()
            for token in ("instagram", "/reel/", "reel/", "igsh"):
                idx = low.find(token)
                if idx != -1:
                    start = max(0, idx - 80)
                    end = min(len(s), idx + 220)
                    snippet = s[start:end].replace("\n", " ")
                    _dbg(f"{name}: ctx[{token}]={snippet}")
                    break

        def _dbg_try_dump(name: str, xml: str) -> None:
            if not debug:
                return
            try:
                os.makedirs(os.path.join("storage", "v3"), exist_ok=True)
                p = os.path.join("storage", "v3", name)
                with open(p, "w", encoding="utf-8", errors="replace") as f:
                    f.write(xml or "")
                _dbg(f"wrote_dump={p}")
            except Exception as e:
                _dbg(f"dump_write_failed={type(e).__name__}")

        def _invalidate_dump_cache() -> None:
            # Important: share-sheet transitions happen fast; without invalidation
            # the short UIAutomator cache may return the pre-tap screen.
            try:
                self._last_ui_dump_ts = 0.0
                self._last_ui_dump_xml = ""
            except Exception:
                pass

        opened_sheet = False
        opened_system_share = False

        # 1) Ensure we're on Reels (avoid tapping blindly in DM/profile/etc.).
        on_reels = False
        try:
            on_reels = bool(self.is_probably_on_reels(retries=2, retry_sleep_s=0.25))
        except Exception:
            on_reels = False
        _dbg(f"prefight_on_reels={on_reels}")

        if not on_reels:
            try:
                if getattr(self, "open_reels", None):
                    ok = bool(self.open_reels())
                    _dbg(f"open_reels_prefight_ok={ok}")
                    time.sleep(1.2)
                    try:
                        on_reels = bool(self.is_probably_on_reels(retries=2, retry_sleep_s=0.25))
                    except Exception:
                        on_reels = False
                    _dbg(f"prefight2_on_reels={on_reels}")
            except Exception:
                pass

        if not on_reels:
            _dbg("not_on_reels_abort")
            return None

        # 2) Open share/send sheet
        xml = self._uiautomator_dump_xml_quick(timeout_dump_s=3.5, timeout_cat_s=2.5) or ""
        _dbg(f"pre_dump_len={len(xml)}")
        _dbg(f"pre_has_direct_share_button={'direct_share_button' in xml}")

        # Prefer known Instagram Reel share button resource-id, but allow variants.
        send_bounds = self._find_bounds_for_any_resource_id_fragment(
            xml,
            [
                "direct_share_button",
                "share_button",
                "reel_share",
                "reels_share",
            ],
        )
        if not send_bounds:
            send_bounds = self._find_bounds_for_any_label(
                xml,
                [
                    "Envoyer",
                    "Send",
                    "Partager",
                    "Share",
                ],
            )
        _dbg(f"send_bounds_by_rid={send_bounds}")
        _dbg(f"send_bounds_final={send_bounds}")
        if not send_bounds:
            # Some UIs only expose an icon without a label; cannot safely tap.
            _dbg("share_button_not_found")
            return None
        tap_ok = self._tap_bounds(send_bounds)
        _dbg(f"tap_share_button_ok={tap_ok}")
        if not tap_ok:
            return None
        opened_sheet = True
        # Give the UI time to transition and force a fresh dump.
        time.sleep(0.9)
        _invalidate_dump_cache()

        # 2) Extract the Reel URL from the *system share* UI dump.
        # On this device/build, the Instagram sheet itself does not expose the URL,
        # but tapping "Partager" opens a system share UI that does.
        def _extract_reel_url_from_text(text: str) -> str | None:
            if not text:
                return None
            for m in re.finditer(r"https?://[^\s\"\']+", text):
                cand = str(m.group(0) or "").strip()
                if not cand:
                    continue
                cand = html.unescape(cand).replace("\\/", "/")
                cand = cand.strip("\"'<>)]}.,; ")
                low = cand.lower()
                if "instagram.com" not in low:
                    continue
                if ("/reel/" not in low) and ("/reels/" not in low):
                    continue
                return cand
            return None

        def _read_clipboard_reel_url() -> str | None:
            # Best-effort clipboard read; not guaranteed on all devices.
            candidates: list[str] = []
            cmds = [
                ["shell", "cmd", "clipboard", "get"],
                ["shell", "cmd", "clipboard", "get", "0"],
                ["shell", "cmd", "clipboard", "get", "--user", "0"],
                ["shell", "dumpsys", "clipboard"],
            ]
            for cmd in cmds:
                try:
                    out = self._run(cmd, timeout=6.0) or ""
                except Exception:
                    out = ""
                if out:
                    candidates.append(out)
                    url = _extract_reel_url_from_text(out)
                    if url:
                        return url
            # Some implementations print raw text with no scheme; last resort parse any line.
            try:
                joined = "\n".join(candidates)
            except Exception:
                joined = ""
            return _extract_reel_url_from_text(joined)
        xml2 = self._uiautomator_dump_xml_quick(timeout_dump_s=3.5, timeout_cat_s=2.5) or ""
        _dbg(f"post_dump_len={len(xml2)}")
        _dbg_xml("share_sheet", xml2)
        _dbg_try_dump("debug_share_sheet.xml", xml2)

        # 2a) Preferred behavior: tap "Copier le lien" / "Copy link" then read clipboard.
        copy_bounds = self._find_bounds_for_any_label(
            xml2,
            [
                "Copier le lien",
                "Copier le lien…",
                "Copier l'URL",
                "Copier l’URL",
                "Copier",
                "Copy link",
                "Copy Link",
                "Copy",
            ],
        )
        if not copy_bounds:
            copy_bounds = self._find_bounds_for_any_resource_id_fragment(
                xml2,
                [
                    "copy",
                    "copy_link",
                    "copylink",
                ],
            )
        _dbg(f"copy_link_bounds={copy_bounds}")
        url = None
        if copy_bounds and self._tap_bounds(copy_bounds):
            time.sleep(0.35)
            _invalidate_dump_cache()
            url = _read_clipboard_reel_url()
            _dbg(f"clipboard_url_found={bool(url)}")
            if url:
                _dbg(f"reel_url_from_clipboard={url}")
            else:
                # If clipboard read not available, we will fall back to system share parsing.
                _dbg("clipboard_read_failed_or_empty")

        share_bounds = self._find_bounds_for_any_label(
            xml2,
            [
                "Partager",
                "Partager via",
                "Share",
                "Share via",
                "Plus",
                "More",
                "Autres",
                "Other",
            ],
        )
        _dbg(f"system_share_bounds={share_bounds}")
        if (not url) and share_bounds and self._tap_bounds(share_bounds):
            opened_system_share = True
            time.sleep(0.8)
            _invalidate_dump_cache()
            xml_sys = self._uiautomator_dump_xml_quick(timeout_dump_s=3.5, timeout_cat_s=2.5) or ""
            url = _extract_reel_url_from_text(xml_sys)
            _dbg(f"after_system_share_url_found={bool(url)}")
            if not url:
                _dbg_xml("system_share", xml_sys)
                _dbg_try_dump("debug_system_share.xml", xml_sys)
            else:
                _dbg(f"reel_url_from_system_share={url}")

        # 4) Faire un seul BACK après la copie d'URL
        try:
            self.press_back()
            time.sleep(0.25)
            _dbg("pressed_back_once_after_copy")
        except Exception:
            pass
        _dbg(f"returning_url={url}")
        return url

    def _uiautomator_dump_xml(self) -> str | None:
        """Return UIAutomator window dump XML (best-effort).

        This is a read-only observation channel used to extract the *real*
        Reel shortcode when possible.
        """
        # Short cache to avoid calling uiautomator multiple times per step.
        try:
            now = float(time.time())
            if self._last_ui_dump_xml and (now - float(self._last_ui_dump_ts or 0.0)) <= 0.8:
                return self._last_ui_dump_xml
        except Exception:
            pass

        # Give the UI a tiny moment to settle; reduces "could not get idle state" on some devices.
        try:
            time.sleep(0.12)
        except Exception:
            pass

        # Best option: print XML to stdout.
        # Some devices/adb builds are flaky here, so do a short retry.
        for _ in range(2):
            out = self._run(["exec-out", "uiautomator", "dump", "/dev/tty"], timeout=12.0)
            if out and ("<?xml" in out or "<hierarchy" in out):
                try:
                    self._last_ui_dump_ts = float(time.time())
                    self._last_ui_dump_xml = out
                except Exception:
                    pass
                return out
            try:
                time.sleep(0.25)
            except Exception:
                pass

        # Fallback: write to file then cat. Also retry once with --compressed.
        dump_cmds = [
            ["shell", "uiautomator", "dump", "--compressed", "/sdcard/window_dump.xml"],
            ["shell", "uiautomator", "dump", "/sdcard/window_dump.xml"],
        ]
        for cmd in dump_cmds:
            ok = self._run_ok(cmd, timeout=20.0)
            if not ok:
                continue

            out2 = self._run(["shell", "cat", "/sdcard/window_dump.xml"], timeout=10.0)
            if out2 and ("<?xml" in out2 or "<hierarchy" in out2):
                try:
                    self._last_ui_dump_ts = float(time.time())
                    self._last_ui_dump_xml = out2
                except Exception:
                    pass
                return out2

            # One more cat attempt (some devices flush the file late).
            try:
                time.sleep(0.2)
            except Exception:
                pass
            out3 = self._run(["shell", "cat", "/sdcard/window_dump.xml"], timeout=10.0)
            if out3 and ("<?xml" in out3 or "<hierarchy" in out3):
                try:
                    self._last_ui_dump_ts = float(time.time())
                    self._last_ui_dump_xml = out3
                except Exception:
                    pass
                return out3

        return None

    def get_current_reel_shortcode(self) -> str | None:
        """Try to extract the current Instagram Reel shortcode from UI dump.

        Returns something like: "C0DeAbC123_" or None if not found.

        Note: This depends on Instagram exposing the URL/shortcode somewhere
        in the view hierarchy; it's best-effort.
        """
        xml = self._uiautomator_dump_xml()
        if not xml:
            return None

        # Common patterns (plain or escaped).
        patterns = [
            r"/reel/([A-Za-z0-9_-]{5,})",
            r"reel/([A-Za-z0-9_-]{5,})",
            r"reel\\/([A-Za-z0-9_-]{5,})",
        ]
        for pat in patterns:
            m = re.search(pat, xml)
            if m:
                code = (m.group(1) or "").strip()
                if code:
                    return code
        return None

    def _require_input_allowed(self) -> None:
        # IMPORTANT: this must honor runtime toggling (Telegram setting) and not just
        # the initial config/env value.
        if not bool(self.input_enabled()):
            raise RuntimeError("ADB input disabled (enable it in V3 settings or set V3_ENABLE_DEVICE_INPUT=1)")

    def get_screen_size(self) -> tuple[int, int] | None:
        """Return (width, height) if available."""
        out = self._run(["shell", "wm", "size"], timeout=5.0)
        if not out:
            return None
        # Typical: "Physical size: 1200x1920"
        for line in out.splitlines():
            if "x" in line and "size" in line.lower():
                parts = line.split(":")[-1].strip().split("x")
                if len(parts) == 2:
                    try:
                        w = int(parts[0].strip())
                        h = int(parts[1].strip())
                        if w > 0 and h > 0:
                            return w, h
                    except Exception:
                        continue
        return None

    def get_foreground_package(self) -> str | None:
        """Best-effort currently focused/resumed package name.

        Used to detect when Instagram lost focus (e.g., external browser/ads).
        """
        # Try window focus first (works on many Android versions).
        out = self._run(["shell", "dumpsys", "window", "windows"], timeout=6.0) or ""
        # Examples:
        #   mCurrentFocus=Window{... u0 com.instagram.android/com.instagram.mainactivity.MainActivity}
        #   mCurrentFocus=Window{... u0 com.android.chrome/com.google.android.apps.chrome.Main}
        m = re.search(r"mCurrentFocus=Window\{[^}]*\s([a-zA-Z0-9_\.]+)\/", out)
        if m:
            pkg = (m.group(1) or "").strip()
            return pkg or None

        # Fallback: resumed activity.
        out2 = self._run(["shell", "dumpsys", "activity", "activities"], timeout=6.0) or ""
        # Example:
        #   ResumedActivity: ActivityRecord{... u0 com.instagram.android/.mainactivity.MainActivity ...}
        m2 = re.search(r"ResumedActivity:.*?\s([a-zA-Z0-9_\.]+)\/", out2)
        if m2:
            pkg = (m2.group(1) or "").strip()
            return pkg or None

        return None

    def tap_and_capture_age(self, x: int, y: int, n: int = 3, delay: float = 0.5, out_dir: str = "storage/v3") -> dict:
        """
        Tap at (x,y) up to `n` times, wait `delay` seconds after each tap,
        capture a screenshot, run OCR (if available) and try to parse a date/age.

        Returns a dict: {"images": [paths], "ocr": [texts], "parsed": str|None, "logs": [lines]}
        """
        import io
        import os
        import subprocess
        import re
        import shutil

        logs: list[str] = []
        images: list[str] = []
        ocr_texts: list[str] = []
        parsed: str | None = None

        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception:
            pass

        base = self._adb_base()

        for i in range(int(n) if n else 1):
            ts = int(time.time())
            try:
                cmd_tap = base + ["shell", "input", "tap", str(int(x)), str(int(y))]
                logs.append(f"tap #{i} cmd={' '.join(cmd_tap)}")
                try:
                    cp = subprocess.run(cmd_tap, capture_output=True, timeout=5.0)
                    logs.append(f"tap #{i} rc={cp.returncode}")
                except Exception as e:
                    logs.append(f"tap #{i} exc={type(e).__name__}:{e}")
                try:
                    time.sleep(float(delay))
                except Exception:
                    pass

                cmd_sc = base + ["exec-out", "screencap", "-p"]
                logs.append(f"screencap #{i} cmd={' '.join(cmd_sc)}")
                try:
                    cp2 = subprocess.run(cmd_sc, capture_output=True, timeout=8.0)
                    if int(cp2.returncode) != 0:
                        logs.append(f"screencap #{i} failed rc={cp2.returncode}")
                        continue
                    img_bytes = cp2.stdout or b""
                    fname = os.path.join(out_dir, f"stv_age_{ts}_{i}.png")
                    try:
                        with open(fname, "wb") as f:
                            f.write(img_bytes)
                        images.append(fname)
                        logs.append(f"saved screenshot #{i}: {fname}")
                    except Exception as e:
                        logs.append(f"save_screenshot_exc #{i}: {e}")

                    # Try OCR if available
                    text = ""
                    try:
                        from PIL import Image
                        import pytesseract
                        # configure tesseract binary if provided
                        tcmd = str(os.getenv("TESSERACT_CMD", "")).strip()
                        if not tcmd:
                            tcmd = str(shutil.which("tesseract") or "").strip()
                        if tcmd:
                            try:
                                pytesseract.pytesseract.tesseract_cmd = tcmd
                            except Exception:
                                pass
                        try:
                            img = Image.open(io.BytesIO(img_bytes))
                            cfg = "--psm 6"
                            try:
                                text = str(pytesseract.image_to_string(img, lang="fra+eng", config=cfg) or "")
                            except Exception:
                                text = str(pytesseract.image_to_string(img, config=cfg) or "")
                            logs.append(f"ocr #{i} len={len(text)}")
                        except Exception as e:
                            logs.append(f"ocr_image_open_exc #{i}: {e}")
                    except Exception:
                        logs.append("ocr unavailable (Pillow/pytesseract missing)")

                    ocr_texts.append(text)

                    # parse common date/age patterns
                    try:
                        m = re.search(r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})", text)
                        if m:
                            parsed = m.group(1)
                            logs.append(f"parsed date: {parsed}")
                            break
                        m2 = re.search(r"(\d{1,3})\s*(ans|years)", text, re.IGNORECASE)
                        if m2:
                            parsed = f"{m2.group(1)} ans"
                            logs.append(f"parsed age: {parsed}")
                            break
                        # fallback AGE_SECONDS line (from age API injection)
                        m3 = re.search(r"AGE_SECONDS=(\d+)", text)
                        if m3:
                            parsed = f"{int(m3.group(1))}s"
                            logs.append(f"parsed AGE_SECONDS: {parsed}")
                            break
                    except Exception as e:
                        logs.append(f"parse_exc: {e}")

                except Exception as e:
                    logs.append(f"screencap_exc #{i}: {type(e).__name__}:{e}")
            except Exception as e:
                logs.append(f"unexpected #{i}: {type(e).__name__}:{e}")

        return {"images": images, "ocr": ocr_texts, "parsed": parsed, "logs": logs}

    def is_probably_on_reels(self, *, retries: int = 3, retry_sleep_s: float = 0.35) -> bool:
        """Heuristic: return True if the current Instagram screen looks like the Reels viewer.

        This is best-effort and relies on UIAutomator dump containing known resource-ids.
        """
        # IMPORTANT: avoid false negatives due to transient/empty dumps.
        # Use fast exec-out dumps with a short timeout and retry a few times.
        xml = ""
        attempts = max(1, int(retries))
        for i in range(attempts):
            try:
                out = self._uiautomator_dump_xml_quick(timeout_dump_s=3.5, timeout_cat_s=2.5) or ""
                if out:
                    xml = out
                    break
            except Exception:
                pass
            if i + 1 < attempts:
                try:
                    time.sleep(float(retry_sleep_s))
                except Exception:
                    pass

        debug = (
            str(os.getenv("V3_ANDROID_DEBUG", "")).strip() == "1"
            or str(os.getenv("V3_DEBUG_ANDROID", "")).strip() == "1"
        )

        def _dbg(msg: str) -> None:
            if not debug:
                return
            try:
                now = float(time.time())
                if (now - float(self._last_reels_dbg_ts or 0.0)) < 2.5:
                    return
                self._last_reels_dbg_ts = now
            except Exception:
                pass
            try:
                print(f"[DEVICE] reels_check {msg}", flush=True)
            except Exception:
                pass

        if not xml:
            _dbg("no_xml")

            # Fallback: UIAutomator can hang on some devices/ROMs. Use dumpsys activity
            # to detect that Instagram was launched into the Reels surface.
            # IMPORTANT: this is a weak signal; only trust it shortly after open_reels().
            try:
                ds = (self._run(["shell", "dumpsys", "activity", "activities"], timeout=4.0) or "").lower()
                # Common when Reels was opened via deep-link.
                if ("instagram://reels" in ds or "instagram://reel" in ds):
                    try:
                        age = float(time.time()) - float(self._last_open_reels_ts or 0.0)
                    except Exception:
                        age = 1e9
                    if age <= 20.0:
                        _dbg("fallback=dumpsys_intent_reels(age_ok)")
                        return True
                    _dbg("fallback=dumpsys_intent_reels(age_too_old)")
            except Exception:
                pass

            # If dumps are failing, do not immediately assume we're off Reels.
            # Grace period after last positive detection.
            try:
                if self._last_reels_yes_ts and (time.time() - float(self._last_reels_yes_ts)) <= 30.0:
                    _dbg("grace_period=yes")
                    return True
            except Exception:
                pass
            return False

        # Heuristics based on resource-id fragments and tab selection.
        # Keep reasonably conservative, but avoid false negatives across UI variants.
        low = xml.lower()

        needles = [
            # Strong, viewer-specific markers.
            "direct_share_button",
            "reel_viewer",
            "reels_viewer",
            "clips_viewer",
            "reel_player",
            "reels_player",
            "clips_player",
            "reel_video",
            "reels_video",
        ]

        ok = any(n in low for n in needles)

        if debug:
            try:
                hit = next((n for n in needles if n in low), "")
                _dbg(f"len={len(low)} ok_by_needles={bool(hit)} hit={hit!r}")
            except Exception:
                pass

        # Additional signal: the Reels/Clips tab is selected.
        # IMPORTANT: do NOT treat mere presence of the tab as being on Reels,
        # because the bottom nav exists on many screens (home/profile).
        if not ok:
            try:
                if re.search(r"(reels|réels|clips).{0,220}(selected|checked)=\"true\"", low, flags=re.DOTALL):
                    ok = True
                elif re.search(r"(selected|checked)=\"true\".{0,220}(reels|réels|clips)", low, flags=re.DOTALL):
                    ok = True
            except Exception:
                ok = False

        if debug and not ok:
            # Show a tiny snippet around the first occurrence of 'reel'/'reels' if present.
            try:
                idx = low.find("reel")
                if idx >= 0:
                    snippet = (low[max(0, idx - 120) : idx + 260]).replace("\n", " ")
                    _dbg(f"snippet={snippet}")
            except Exception:
                pass

        if ok:
            try:
                self._last_reels_yes_ts = float(time.time())
            except Exception:
                pass
        return bool(ok)

    def launch_instagram(self) -> bool:
        """Bring Instagram to foreground (best-effort)."""
        self._require_input_allowed()
        pkg = str(self._cfg.instagram_package or "com.instagram.android").strip() or "com.instagram.android"
        # Prefer a direct launch intent; fallback to monkey.
        ok = self._run_ok(["shell", "am", "start", "-n", f"{pkg}/com.instagram.mainactivity.MainActivity"], timeout=8.0)
        if ok:
            return True
        return self._run_ok(
            ["shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"],
            timeout=8.0,
        )

    def open_reels(self) -> bool:
        """Best-effort navigation to Reels inside Instagram.

        This is not pixel-perfect; it relies on intent/deep-linking.
        """
        self._require_input_allowed()
        pkg = str(self._cfg.instagram_package or "com.instagram.android").strip() or "com.instagram.android"

        def _tap_reels_tab_ui() -> bool:
            # Fallback when deep-links are blocked or land on the wrong screen.
            # We try labels first (French/English), then resource-id fragments.
            xml = self._uiautomator_dump_xml() or ""
            if not xml:
                return False

            bounds = self._find_bounds_for_any_label(
                xml,
                [
                    "Reels",
                    "Réels",
                    "REELS",
                    "RÉELS",
                    "Clips",
                ],
            )
            if not bounds:
                bounds = self._find_bounds_for_any_resource_id_fragment(
                    xml,
                    [
                        "reels",
                        "reel",
                        "clips",
                        "tab_reels",
                        "reels_tab",
                        "clips_tab",
                    ],
                )
            if not bounds:
                return False

            return bool(self._tap_bounds(bounds))

        # Record that we attempted to navigate to Reels (used for safe fallbacks).
        try:
            self._last_open_reels_ts = float(time.time())
        except Exception:
            pass

        # 1) Try Instagram URI scheme first, then fallback to common web URL.
        for url in ("instagram://reels", "instagram://reel", "https://www.instagram.com/reels/"):
            ok = self._run_ok(
                ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url, "-p", pkg],
                timeout=10.0,
            )
            if ok:
                try:
                    time.sleep(1.0)
                except Exception:
                    pass
                # If it actually landed on Reels, we're done.
                try:
                    if self.is_probably_on_reels():
                        return True
                except Exception:
                    return True

        # 2) UI fallback: try tapping the Reels tab/button.
        try:
            if _tap_reels_tab_ui():
                time.sleep(1.0)
                return bool(self.is_probably_on_reels())
        except Exception:
            return False

        return False

    def swipe_up(self) -> bool:
        """Visible scroll gesture."""
        self._require_input_allowed()
        w, h = self.get_screen_size() or (720, 1280)
        margin = max(0.05, min(0.45, float(self._cfg.swipe_margin_ratio)))
        x = int(w / 2)
        y1 = int(h * (1.0 - margin))
        y2 = int(h * margin)
        dur = max(120, int(self._cfg.swipe_duration_ms))
        ok = self._run_ok(["shell", "input", "swipe", str(x), str(y1), str(x), str(y2), str(dur)], timeout=5.0)

        # Ensure next UI dump isn't stale.
        try:
            self._last_ui_dump_ts = 0.0
            self._last_ui_dump_xml = ""
        except Exception:
            pass

        return bool(ok)

    def tap_center(self) -> bool:
        """Best-effort 'open' gesture (tap center)."""
        self._require_input_allowed()
        if not bool(self._cfg.tap_to_open):
            return True
        w, h = self.get_screen_size() or (720, 1280)
        x = int(w / 2)
        y = int(h / 2)
        return self._run_ok(["shell", "input", "tap", str(x), str(y)], timeout=4.0)

    def _is_connected(self) -> bool:
        out = self._run(["get-state"], timeout=4.0)
        if not out:
            return False
        return "device" in out.lower()

    def _boot_completed(self) -> bool:
        out = self._run(["shell", "getprop", "sys.boot_completed"], timeout=4.0)
        return bool(out and out.strip() == "1")

    def _is_locked(self) -> bool:
        out = self._run(["shell", "dumpsys", "window"], timeout=6.0)
        if not out:
            return True

        s = out

        if "isKeyguardShowing=true" in s:
            return True
        if "isKeyguardShowing=false" in s:
            pass

        strong_true = [
            "mShowingLockscreen=true",
            "mDreamingLockscreen=true",
            "keyguardShowing=true",
            "isStatusBarKeyguard=true",
            "mShowingDream=true",
        ]
        for n in strong_true:
            if n in s:
                return True

        if "isKeyguardShowing=false" in s or "mShowingLockscreen=false" in s or "keyguardShowing=false" in s:
            return False

        for line in s.splitlines():
            if "mCurrentFocus" in line and "Keyguard" in line:
                return True

        return False

    def get_status(self) -> AndroidStatus:
        if not self._is_connected():
            return "DISCONNECTED"

        if not self._boot_completed():
            return "LOCKED"

        if self._is_locked():
            return "LOCKED"

        return "READY"
