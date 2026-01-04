from __future__ import annotations

import json
import os
import threading
import time
import queue
import sys
from pathlib import Path
import requests

from ..config import load_v3_config
from ..android_agent import AndroidAgent, AndroidAgentConfig
from ..mobile_agent import RiskEstimator, SimulatedMobileAgent
from ..selector import Selector
from ..storage import load_state_locked, save_state_locked, update_state_locked
from .handlers import HandlerDeps, is_allowed, like_if_approved, set_status, start_session, step_once, stop_session
from .menus import render_home, render_item, render_list, render_settings, render_settings_legacy, render_settings_virality
from ..stv_refresh import _adb_base


WELCOME_TEXT = (
    "Bienvenue 👋\n"
    "\n"
    "Objet du bot (V3)\n"
    "- Agent mobile SIMULÉ (aucun ADB, aucune automatisation réelle)\n"
    "- Générer des événements (scroll/open/pause), estimer un score, persister\n"
    "- Persister immédiatement chaque vidéo analysée\n"
    "- Valider humainement via Telegram (approve/reject)\n"
    "- Like SIMULÉ possible UNIQUEMENT après approval\n"
    "\n"
    "Commandes\n"
    "- /start : afficher l’accueil + le menu\n"
    "- /v3 : afficher l’accueil + le menu\n"
    "\n"
    "Utilisation\n"
    "1) Clique ▶️ Démarrer session\n"
    "2) Le bot scroll/observe et enregistre\n"
    "3) Valide les pending dans Telegram\n"
    "\n"
    "Note\n"
    "- Par défaut: aucune action visible sur la tablette (simulation).\n"
    "- Pour actions visibles: définir V3_ENABLE_DEVICE_INPUT=1 (ADB input).\n"
)


def _tg_log_enabled() -> bool:
    # Default ON: user asked for terminal logs.
    return str(os.getenv("V3_TG_LOG", "1")).strip() != "0"


def _short(s: str, n: int = 160) -> str:
    s = str(s or "")
    s = s.replace("\r", " ").replace("\n", " ")
    if len(s) <= n:
        return s
    return s[: n - 3] + "..."


def _log_tg(line: str) -> None:
    if _tg_log_enabled():
        print(line, flush=True)


def _tg_api(token: str, method: str, *, params=None, data=None, files=None, timeout: float = 60.0):
    url = f"https://api.telegram.org/bot{token}/{method}"

    # Outbound Telegram logging (skip long-poll getUpdates).
    try:
        if _tg_log_enabled() and method != "getUpdates":
            chat_id = None
            text = None
            if isinstance(data, dict):
                chat_id = data.get("chat_id")
                # Avoid logging huge payloads (reply_markup/json).
                text = data.get("text") or data.get("caption")
            suffix = ""
            if text is not None:
                suffix = f" text={_short(str(text), 180)!r}"
            _log_tg(f"[TG][OUT] {method} chat_id={chat_id}{suffix}")
    except Exception:
        pass

    resp = requests.post(url, params=params, data=data, files=files, timeout=timeout)
    try:
        payload = resp.json()
    except Exception:
        payload = {"ok": False, "description": resp.text}
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error calling {method}: {payload}")
    return payload.get("result")


def _preview_keyboard(vid: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approuver", "callback_data": f"v3:item:approve:{vid}"},
                {"text": "❌ Rejeter", "callback_data": f"v3:item:reject:{vid}"},
            ],
            [
                {"text": "🗑 Supprimer", "callback_data": f"v3:item:delete:{vid}"},
                {"text": "⬅️ Retour", "callback_data": "v3:home"},
            ],
            [
                {"text": "🧮 Calculer STV", "callback_data": f"v3:item:stv:{vid}"},
            ],
        ]
    }


def _upgrade_recent_previews_add_stv_button(cfg, *, chat_id: int, limit: int = 20) -> None:
    """Best-effort: upgrade old already-sent preview messages to include the STV button.

    Strictly additive: only edits Telegram messages (text+reply_markup) using existing data.
    Does not modify selection logic or storage.
    """
    try:
        st = load_state_locked(cfg.state_file)
        items = list((st.videos or {}).values())
        items.sort(key=lambda vv: float(getattr(vv, "timestamp", 0.0) or 0.0), reverse=True)

        upgraded = 0
        for v in items:
            if upgraded >= int(limit):
                break
            try:
                status = str(getattr(v, "status", "") or "").strip().lower()
                if status not in {"pending", "approved"}:
                    continue
                if int(getattr(v, "message_chat_id", 0) or 0) != int(chat_id):
                    continue
                if not getattr(v, "message_id", None):
                    continue

                print(f"[STV] upgrade_preview_keyboard vid={getattr(v, 'internal_id', None)} mid={getattr(v, 'message_id', None)}", flush=True)
                _try_edit_preview(cfg, v)
                upgraded += 1
            except Exception:
                continue
        print(f"[STV] upgrade_preview_keyboard done upgraded={upgraded}", flush=True)
        save_state_locked(cfg.state_file, st)
    except Exception as e:
        print(f"[STV] upgrade_preview_keyboard failed {type(e).__name__}: {e}", flush=True)


def _caption_for(v) -> str:
    """Telegram preview text for a VideoItem.

    Goal: include all virality/scoring details and keep the Instagram URL present
    so Telegram can (when supported) generate a link preview.
    """

    meta = getattr(v, "meta", None)
    url = ""
    if isinstance(meta, dict):
        url = str(meta.get("clipboard_url") or "").strip()
    if not url:
        url = str(getattr(v, "source_url", None) or getattr(v, "url", None) or "").strip()

    status = str(getattr(v, "status", "") or "").strip() or "unknown"
    try:
        score = float(getattr(v, "score", 0.0) or 0.0)
    except Exception:
        score = 0.0
    try:
        threshold = float(getattr(v, "threshold", 0.0) or 0.0)
    except Exception:
        threshold = 0.0

    reason = str(getattr(v, "reason", "") or "").strip()
    title = str(getattr(v, "title", "") or "").strip()

    try:
        score_latent = float(getattr(v, "score_latent", 0.0) or 0.0)
    except Exception:
        score_latent = 0.0
    try:
        score_viral = float(getattr(v, "score_viral", 0.0) or 0.0)
    except Exception:
        score_viral = 0.0

    viral_label = str(getattr(v, "viral_label", "") or "").strip().upper()

    details = getattr(v, "score_details", None)
    details = dict(details) if isinstance(details, dict) else {}

    def _f2(x: object) -> str:
        try:
            return f"{float(x):.2f}"
        except Exception:
            return str(x)

    rythme = details.get("rythme")
    banalite = details.get("banalite")
    potentiel = details.get("potentiel_viral")

    label_lines: list[str] = []
    try:
        if rythme is not None:
            label_lines.append("Rythme dynamique" if float(rythme) >= 0.50 else "Rythme lent")
    except Exception:
        pass
    try:
        if banalite is not None:
            label_lines.append("Banalité élevée" if float(banalite) >= 0.55 else "Banalité faible")
    except Exception:
        pass
    try:
        if potentiel is not None:
            label_lines.append("Potentiel viral élevé" if float(potentiel) >= 0.75 else "Potentiel viral faible")
    except Exception:
        pass

    hashtags = getattr(v, "hashtags", None)
    hashtags_str = ""
    try:
        if isinstance(hashtags, list):
            hashtags_str = " ".join([str(x) for x in hashtags if str(x).strip()])
    except Exception:
        hashtags_str = ""

    device_actions = []
    try:
        if isinstance(meta, dict) and isinstance(meta.get("device_actions"), list):
            device_actions = [str(x) for x in (meta.get("device_actions") or []) if str(x).strip()]
    except Exception:
        device_actions = []

    if "LATENT" in viral_label:
        header = "💎 VIDÉO LATENTE"
    elif "VIRAL" in viral_label or score >= threshold:
        header = "🔥 VIDÉO DÉJÀ VIRALE"
    else:
        header = "🎬 VIDÉO"

    lines: list[str] = [header]
    lines.append(f"Score latent : {score_latent:.2f}")
    lines.append(f"Score viral : {score_viral:.2f}")
    lines.extend(label_lines)
    if url:
        lines.append(f"🔗 {url}")
    lines.append(f"📈 Score: {score:.2f} (seuil {threshold:.2f})")
    if reason:
        lines.append(f"🧠 Raison: {reason}")
    if details:
        lines.append(
            f"🧾 Détails: rythme={_f2(rythme)} banalite={_f2(banalite)} potentiel_viral={_f2(potentiel)}"
        )
    if title:
        lines.append(f"📝 Titre: {title}")
    if hashtags_str:
        lines.append(hashtags_str)
    if device_actions:
        lines.append(f"📱 Device (attendu): {', '.join(device_actions)}")
    lines.append(f"✅ Statut: {status}")

    caption = "\n".join([ln for ln in lines if str(ln or "").strip()]).strip()

    # --- STRICTLY ADDITIVE TEMPORAL ANALYSIS (V3) ---
    # Constraints:
    # - No changes to selection logic, storage, or flow.
    # - Only for retained items (pending/approved).
    # - Best-effort, non-blocking; if OCR isn't available, show N/A (no approximations).
    try:
        status2 = str(getattr(v, "status", "") or "").strip().lower()
        is_retained = status2 in {"pending", "approved"}
    except Exception:
        is_retained = False

    if caption and is_retained:
        try:
            from ..temporal_analysis import analyze_from_meta, format_telegram_block

            meta2 = getattr(v, "meta", None)
            meta2 = dict(meta2) if isinstance(meta2, dict) else {}
            try:
                _log_tg(f"[AGE][CAPTION] vid={getattr(v, 'internal_id', None)} meta_keys={list(meta2.keys())} meta_sample={_short(str(meta2.get('ocr_raw_text') or meta2.get('ocr_pub') or ''), 120)!r}")
            except Exception:
                pass
            analysis = analyze_from_meta(meta=meta2)
            try:
                _log_tg(f"[AGE][CAPTION] vid={getattr(v, 'internal_id', None)} analysis.age_minutes={getattr(analysis, 'age_minutes', None)} analysis.stv={getattr(analysis, 'stv', None)} analysis.ocr_source={getattr(analysis, 'ocr_source', None)}")
            except Exception:
                pass
            block = format_telegram_block(analysis)
            if str(block or "").strip():
                caption = caption + "\n\n" + block
        except Exception:
            # Never break existing reporting.
            pass

    return caption


def _send_preview(cfg, st, v) -> None:
    debug = str(os.getenv("V3_TG_DEBUG", "")).strip() == "1"
    if debug:
        print(
            f"[DEBUG] _send_preview: internal_id={getattr(v, 'internal_id', None)} "
            f"status={getattr(v, 'status', None)} message_id={getattr(v, 'message_id', None)} "
            f"chat_id={getattr(st, 'control_chat_id', None)} url={getattr(v, 'source_url', None)}",
            flush=True,
        )
    if getattr(v, "message_id", None):
        if debug:
            print("[DEBUG] _send_preview: message_id already set, skipping send.", flush=True)
        return
    chat_id = int(st.control_chat_id) if st.control_chat_id is not None else None
    if chat_id is None:
        if debug:
            print("[DEBUG] _send_preview: chat_id is None, cannot send.", flush=True)
        return

    caption = _caption_for(v)
    try:
        url_ok = False
        cap = str(caption or "")
        if "http://" in cap or "https://" in cap:
            url_ok = True
        first_line = str((caption or "").splitlines()[0] if caption else "").strip()
        _log_tg(
            f"[TG][PREVIEW] vid={getattr(v, 'internal_id', None)} status={getattr(v, 'status', None)} url_ok={url_ok} url={_short(first_line, 140)!r}"
        )
    except Exception:
        pass
    if not str(caption or "").strip():
        if debug:
            print(
                f"[DEBUG] _send_preview: skip send (empty text) internal_id={getattr(v, 'internal_id', None)} "
                f"status={getattr(v, 'status', None)}",
                flush=True,
            )
        # If we cannot send the required URL-only notification, do not keep this item pending,
        # otherwise the session loop will retry forever on every tick.
        try:
            v.status = "deleted"  # type: ignore[assignment]
            meta = getattr(v, "meta", None)
            if isinstance(meta, dict):
                meta["no_url"] = True
        except Exception:
            pass
        return
    kb = _preview_keyboard(v.internal_id)
    reply_markup = json.dumps(kb, ensure_ascii=False)

    try:
        r = _tg_api(
            cfg.telegram_token,
            "sendMessage",
            data={
                "chat_id": str(chat_id),
                "text": caption or f"Item: {v.internal_id}",
                "disable_web_page_preview": "false",
                "reply_markup": reply_markup,
            },
        )
        if debug:
            print(f"[DEBUG] _send_preview: Telegram API response: {r}", flush=True)
        if isinstance(r, dict) and r.get("message_id"):
            v.message_chat_id = int(chat_id)
            v.message_id = int(r.get("message_id"))
    except Exception as e:
        if debug:
            print(f"[DEBUG] _send_preview: Exception during send: {e}", flush=True)


def _send_preview_to_chat(cfg, chat_id: int, v, *, force: bool = False) -> None:
    """Send a preview to a specific chat (used for 'Open' actions).

    If force=True, resend even if a preview already exists and overwrite
    message tracking to this new message.
    """
    if (not force) and getattr(v, "message_id", None):
        return

    caption = _caption_for(v)
    try:
        first_line = str((caption or "").splitlines()[0] if caption else "").strip()
        _log_tg(
            f"[TG][PREVIEW] force_send chat_id={int(chat_id)} vid={getattr(v, 'internal_id', None)} url={_short(first_line, 140)!r}"
        )
    except Exception:
        pass
    if not str(caption or "").strip():
        return
    kb = _preview_keyboard(v.internal_id)
    reply_markup = json.dumps(kb, ensure_ascii=False)
    r = _tg_api(
        cfg.telegram_token,
        "sendMessage",
        data={
            "chat_id": str(int(chat_id)),
            "text": caption or f"Item: {v.internal_id}",
            "disable_web_page_preview": "false",
            "reply_markup": reply_markup,
        },
    )
    if isinstance(r, dict) and r.get("message_id"):
        v.message_chat_id = int(chat_id)
        v.message_id = int(r.get("message_id"))


def _try_edit_preview(cfg, v) -> None:
    chat_id = getattr(v, "message_chat_id", None)
    message_id = getattr(v, "message_id", None)
    if not chat_id or not message_id:
        return
    caption = _caption_for(v)
    kb = _preview_keyboard(v.internal_id)
    try:
        _tg_api(
            cfg.telegram_token,
            "editMessageText",
            data={
                "chat_id": str(int(chat_id)),
                "message_id": str(int(message_id)),
                "text": caption,
                "disable_web_page_preview": "false",
                "reply_markup": json.dumps(kb, ensure_ascii=False),
            },
        )
    except Exception:
        # Backward compat: older messages might be videos -> edit caption.
        try:
            _tg_api(
                cfg.telegram_token,
                "editMessageCaption",
                data={
                    "chat_id": str(int(chat_id)),
                    "message_id": str(int(message_id)),
                    "caption": caption,
                    "reply_markup": json.dumps(kb, ensure_ascii=False),
                },
            )
        except Exception:
            return


def _send_or_edit(token: str, chat_id: int, *, text: str, reply_markup: dict | None, message_id: int | None) -> int:
    if message_id is None:
        r = _tg_api(token, "sendMessage", data={"chat_id": str(chat_id), "text": text, "reply_markup": (None if reply_markup is None else __import__("json").dumps(reply_markup, ensure_ascii=False))})
        return int(r["message_id"]) if isinstance(r, dict) and "message_id" in r else 0

    try:
        _tg_api(
            token,
            "editMessageText",
            data={
                "chat_id": str(chat_id),
                "message_id": str(int(message_id)),
                "text": text,
                "reply_markup": (None if reply_markup is None else __import__("json").dumps(reply_markup, ensure_ascii=False)),
            },
        )
        return int(message_id)
    except Exception as e:
        # Telegram returns a 400 error when the content is identical:
        # "Bad Request: message is not modified". Treat it as success.
        msg = str(e)
        if "message is not modified" in msg:
            return int(message_id)

        r = _tg_api(token, "sendMessage", data={"chat_id": str(chat_id), "text": text, "reply_markup": (None if reply_markup is None else __import__("json").dumps(reply_markup, ensure_ascii=False))})
        return int(r["message_id"]) if isinstance(r, dict) and "message_id" in r else 0


def run() -> None:
    cfg = load_v3_config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    try:
        build_ts = float(Path(__file__).stat().st_mtime)
        build_id = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(build_ts))
    except Exception:
        build_id = "unknown"

    print(
        f"[V3] started build={build_id} python={sys.executable} "
        f"device_input={'ON' if cfg.enable_device_input else 'OFF'} "
        f"state_file={cfg.state_file}",
        flush=True,
    )

    try:
        import os as _os
        proc_id = int(_os.getpid())
    except Exception:
        proc_id = 0

    deps = HandlerDeps(
        state_file=cfg.state_file,
        agent=SimulatedMobileAgent(),
        android_agent=AndroidAgent(
            AndroidAgentConfig(
                adb_path=cfg.adb_path,
                serial=cfg.adb_serial,
                allow_input=bool(cfg.enable_device_input),
                instagram_package=str(cfg.instagram_package),
                swipe_duration_ms=int(cfg.swipe_duration_ms),
                swipe_margin_ratio=float(cfg.swipe_margin_ratio),
                tap_to_open=bool(cfg.tap_to_open),
            )
        ),
        selector=Selector(),
        risk_estimator=RiskEstimator(max_session_seconds=cfg.max_session_seconds),
    )

    # Minimal persisted UI: keep one control message per chat.
    # We store it in memory only; if the process restarts, /start recreates it.
    control_message_ids: dict[int, int] = {}

    def send_welcome(chat_id: int) -> None:
        text = (
            WELCOME_TEXT
            + "\n"
            + f"Build: {build_id}\n"
            + f"PID: {proc_id}\n"
            + f"Python: {sys.executable}\n"
            + f"Device input: {'ON' if cfg.enable_device_input else 'OFF'}\n"
            + f"ADB: {cfg.adb_path} serial={cfg.adb_serial or '-'}"
        )
        _tg_api(cfg.telegram_token, "sendMessage", data={"chat_id": str(chat_id), "text": text})

    def show_purge_videos_confirm(chat_id: int, *, force_new: bool = False) -> None:
        text = (
            "⚠️ Confirmation — Supprimer toutes les vidéos (V3)\n"
            "\n"
            "Cette action efface la liste des vidéos (pending/approved/rejected/deleted)\n"
            "et les métriques de session.\n"
            "Les réglages (Settings) sont conservés.\n"
            "\n"
            "Confirmer ?"
        )
        kb = {
            "inline_keyboard": [
                [
                    {"text": "✅ Oui, supprimer", "callback_data": "v3:videos:purge:yes"},
                    {"text": "❌ Non", "callback_data": "v3:videos:purge:no"},
                ],
                [{"text": "⬅️ Retour menu", "callback_data": "v3:home"}],
            ]
        }
        mid = None if force_new else control_message_ids.get(chat_id)
        new_mid = _send_or_edit(cfg.telegram_token, chat_id, text=text, reply_markup=kb, message_id=mid)
        if new_mid:
            control_message_ids[chat_id] = new_mid

    def _purge_all_videos_state(chat_id: int) -> None:
        try:
            st2 = load_state_locked(cfg.state_file)
            st2.videos = {}
            st2.session_metrics = {}
            st2.last_session_stop_reason = None
            save_state_locked(cfg.state_file, st2)
            _tg_api(cfg.telegram_token, "sendMessage", data={"chat_id": str(chat_id), "text": "🗑 Toutes les vidéos ont été supprimées (V3)."})
        except Exception:
            _tg_api(cfg.telegram_token, "sendMessage", data={"chat_id": str(chat_id), "text": "⚠️ Suppression impossible (erreur)."})

    def show_home(chat_id: int, *, force_new: bool = False):
        st = load_state_locked(cfg.state_file)
        # Refresh device status for visibility in the home screen.
        try:
            if deps.android_agent is not None:
                # Avoid blocking /start on slow ADB calls: refresh at most every 10s.
                now = float(time.time())
                last = float(getattr(st, "device_status_ts", 0.0) or 0.0)
                if (not getattr(st, "device_status", None)) or ((now - last) >= 10.0):
                    st.device_status = deps.android_agent.get_status()
                    st.device_status_ts = now
                    save_state_locked(cfg.state_file, st)
        except Exception:
            pass
        text, kb = render_home(st)
        mid = None if force_new else control_message_ids.get(chat_id)
        new_mid = _send_or_edit(cfg.telegram_token, chat_id, text=text, reply_markup=kb, message_id=mid)
        if new_mid:
            control_message_ids[chat_id] = new_mid

    def show_list(chat_id: int, which: str, *, page: int = 0):
        st = load_state_locked(cfg.state_file)
        text, kb = render_list(st, which, page=page)
        mid = control_message_ids.get(chat_id)
        new_mid = _send_or_edit(cfg.telegram_token, chat_id, text=text, reply_markup=kb, message_id=mid)
        if new_mid:
            control_message_ids[chat_id] = new_mid

    def show_item(chat_id: int, vid: str):
        st = load_state_locked(cfg.state_file)
        v = st.videos.get(vid)
        if not v:
            _tg_api(cfg.telegram_token, "sendMessage", data={"chat_id": str(chat_id), "text": "Not found."})
            return
        text, kb = render_item(v)
        mid = control_message_ids.get(chat_id)
        new_mid = _send_or_edit(cfg.telegram_token, chat_id, text=text, reply_markup=kb, message_id=mid)
        if new_mid:
            control_message_ids[chat_id] = new_mid

    def _ensure_settings_defaults(st) -> bool:
        s = getattr(st, "settings", {})
        s = dict(s) if isinstance(s, dict) else {}
        changed = False

        def _set_default(key: str, value):
            nonlocal changed
            if key not in s:
                s[key] = value
                changed = True

        # Selector / scoring
        _set_default("score_threshold", 0.65)
        _set_default("weight_banalite", 0.35)
        _set_default("weight_potentiel_viral", 0.35)
        _set_default("weight_rythme", 0.30)
        _set_default("rythme_target", 0.45)

        # Parallel categorization: viral already active
        _set_default("threshold_viral", 0.72)
        _set_default("viral_w_banalite", 0.30)
        _set_default("viral_w_potentiel_viral", 0.45)
        _set_default("viral_w_rythme", 0.25)
        _set_default("viral_rythme_target", 0.50)

        # Parallel categorization: viral latent
        _set_default("threshold_latent", 0.60)
        _set_default("latent_w_banalite", 0.45)
        _set_default("latent_w_potentiel_viral", 0.20)
        _set_default("latent_w_rythme", 0.35)
        _set_default("latent_rythme_target", 0.38)

        # Timing
        _set_default("scroll_pause_seconds", 0.8)
        _set_default("open_watch_min_seconds", 1.5)
        _set_default("open_watch_max_seconds", 4.0)
        _set_default("loop_sleep_seconds", float(cfg.step_sleep_seconds))

        # Device-visible actions (ADB input). Default follows env/config, but can be changed from Telegram.
        _set_default("device_input_enabled", bool(cfg.enable_device_input))

        # STV / OCR tuning (used by the Telegram "🧮 Calculer STV" button)
        _set_default("stv_ocr_tries", 3)
        _set_default("stv_right_crop_x0_ratio", 0.72)
        _set_default("stv_right_crop_y0_ratio", 0.55)
        _set_default("stv_right_crop_y1_ratio", 0.97)
        _set_default("stv_max_views_like_ratio", 500.0)
        _set_default("stv_abs_max_views", 200_000_000)

        # Risk safety: when enabled, the bot may auto-stop the session on HIGH_RISK.
        _set_default("risk_safety_enabled", True)

        # Stop conditions
        # Stop after N items have actually been sent to Telegram (preview message created).
        if "target_sent_per_session" not in s:
            # Backward compatibility: migrate old key if it exists.
            if "target_kept_per_session" in s:
                s["target_sent_per_session"] = s.get("target_kept_per_session")
                changed = True
            else:
                s["target_sent_per_session"] = 10
                changed = True

        if changed:
            st.settings = s
        return changed

    def show_settings(chat_id: int, *, force_new: bool = False, page: str = "main"):
        st = load_state_locked(cfg.state_file)
        if _ensure_settings_defaults(st):
            save_state_locked(cfg.state_file, st)
        page = str(page or "main").strip().lower()
        if page == "legacy":
            text, kb = render_settings_legacy(st)
        elif page in {"virality", "viral", "latent"}:
            text, kb = render_settings_virality(st)
        else:
            text, kb = render_settings(st)
        mid = None if force_new else control_message_ids.get(chat_id)
        new_mid = _send_or_edit(cfg.telegram_token, chat_id, text=text, reply_markup=kb, message_id=mid)
        if new_mid:
            control_message_ids[chat_id] = new_mid

    def _apply_setting_delta(chat_id: int, key: str, delta: float | int) -> None:
        st = load_state_locked(cfg.state_file)
        if _ensure_settings_defaults(st):
            pass
        s = dict(getattr(st, "settings", {}) or {})

        def _clampf(x: float, lo: float, hi: float) -> float:
            return max(lo, min(hi, float(x)))

        if key in {
            "score_threshold",
            "weight_banalite",
            "weight_potentiel_viral",
            "weight_rythme",
            "rythme_target",
            "threshold_viral",
            "viral_w_banalite",
            "viral_w_potentiel_viral",
            "viral_w_rythme",
            "viral_rythme_target",
            "threshold_latent",
            "latent_w_banalite",
            "latent_w_potentiel_viral",
            "latent_w_rythme",
            "latent_rythme_target",
        }:
            try:
                cur = float(s.get(key, 0.0) or 0.0)
            except Exception:
                cur = 0.0
            s[key] = _clampf(cur + float(delta), 0.0, 1.0)
        elif key in {"scroll_pause_seconds", "loop_sleep_seconds", "open_watch_min_seconds", "open_watch_max_seconds"}:
            try:
                cur = float(s.get(key, 0.0) or 0.0)
            except Exception:
                cur = 0.0
            # Keep sane bounds (seconds).
            s[key] = _clampf(cur + float(delta), 0.05, 60.0)
        elif key == "target_sent_per_session":
            try:
                cur_i = int(s.get(key, 0) or 0)
            except Exception:
                cur_i = 0
            s[key] = int(_clampf(cur_i + int(delta), 0, 500))

        # STV / OCR tuning
        elif key == "stv_ocr_tries":
            try:
                cur_i = int(s.get(key, 0) or 0)
            except Exception:
                cur_i = 0
            s[key] = int(_clampf(cur_i + int(delta), 1, 5))
        elif key in {"stv_right_crop_x0_ratio", "stv_right_crop_y0_ratio", "stv_right_crop_y1_ratio"}:
            try:
                cur = float(s.get(key, 0.0) or 0.0)
            except Exception:
                cur = 0.0
            # Ratios are in [0..1]. Keep conservative bounds.
            if key == "stv_right_crop_x0_ratio":
                s[key] = _clampf(cur + float(delta), 0.40, 0.95)
            elif key == "stv_right_crop_y0_ratio":
                s[key] = _clampf(cur + float(delta), 0.20, 0.95)
            else:  # y1
                s[key] = _clampf(cur + float(delta), 0.50, 1.00)
        elif key == "stv_max_views_like_ratio":
            try:
                cur = float(s.get(key, 0.0) or 0.0)
            except Exception:
                cur = 0.0
            s[key] = _clampf(cur + float(delta), 10.0, 1_000_000.0)
        elif key == "stv_abs_max_views":
            try:
                cur_i = int(s.get(key, 0) or 0)
            except Exception:
                cur_i = 0
            s[key] = int(_clampf(cur_i + int(delta), 10_000, 2_000_000_000))

        st.settings = s
        save_state_locked(cfg.state_file, st)

    def session_loop():
        last_loop_log_ts = 0.0
        last_loop_state = ""
        while True:
            try:
                st = load_state_locked(cfg.state_file)
                now = float(time.time())
                state = "idle"
                if st.active_session_id and st.control_chat_id is not None:
                    state = "paused" if bool(getattr(st, "session_paused", False)) else "active"

                # Log state transitions, and a periodic heartbeat when active.
                try:
                    if state != last_loop_state:
                        print(
                            f"[V3][LOOP] state={state} sid={st.active_session_id or '-'} chat_id={st.control_chat_id or '-'}",
                            flush=True,
                        )
                        last_loop_state = state
                        last_loop_log_ts = now
                    elif state == "active" and (now - float(last_loop_log_ts or 0.0)) >= 5.0:
                        print(
                            f"[V3][LOOP] stepping sid={st.active_session_id} chat_id={st.control_chat_id}",
                            flush=True,
                        )
                        last_loop_log_ts = now
                except Exception:
                    pass

                if not st.active_session_id or st.control_chat_id is None:
                    time.sleep(1.0)
                    continue

                # Pause: keep session id but stop stepping.
                if bool(getattr(st, "session_paused", False)):
                    time.sleep(1.0)
                    continue

                prev_level = (st.last_risk_level or "SAFE")
                prev_active = st.active_session_id

                # One step; persist inside handler.
                st2, item, risk = step_once(deps)

                # Always try to send ONE preview for any pending item without message_id.
                # This covers restarts where pending items exist but had no notification.
                try:
                    candidates = [
                        v
                        for v in (st2.videos.values() if hasattr(st2, "videos") else [])
                        if getattr(v, "status", None) == "pending" and not getattr(v, "message_id", None)
                    ]
                    candidates.sort(key=lambda v: float(getattr(v, "timestamp", 0.0) or 0.0), reverse=True)
                    if candidates:
                        cand = candidates[0]
                        before_mid = getattr(cand, "message_id", None)
                        # Attempt to fetch reel age before sending preview so STV/Telegram block
                        # can use an explicit AGE_SECONDS override discovered from Instagram.
                        try:
                            url = ""
                            try:
                                metau = dict(getattr(cand, "meta", {}) or {})
                                url = str(metau.get("clipboard_url") or cand.source_url or "").strip()
                            except Exception:
                                url = str(getattr(cand, "source_url", "") or "")
                            if url:
                                try:
                                    import created_time_test as _ctt

                                    age_s = None
                                    try:
                                        age_s = _ctt.get_reel_age_seconds(url)
                                    except Exception:
                                        age_s = None
                                    if age_s is not None:
                                        try:
                                            cand.meta = dict(getattr(cand, "meta", {}) or {})
                                            # Persist both a machine-readable hint and explicit raw text
                                            cand.meta["reel_age_seconds"] = int(age_s)
                                            cand.meta["reel_created_ts"] = int(__import__("time").time()) - int(age_s)
                                            # Inject an override that parse_relative_pub_time recognizes
                                            old_raw = str(cand.meta.get("ocr_raw_text") or "").strip()
                                            extra = f"AGE_SECONDS = {int(age_s)}"
                                            cand.meta["ocr_raw_text"] = (old_raw + " " + extra).strip()
                                            save_state_locked(cfg.state_file, st2)
                                            try:
                                                _log_tg(f"[AGE][PRE-SEND] vid={getattr(cand, 'internal_id', None)} url={url} age_s={int(age_s)}")
                                            except Exception:
                                                pass
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                        except Exception:
                            pass

                        _send_preview(cfg, st2, cand)
                        save_state_locked(cfg.state_file, st2)

                        # If we just created a Telegram message for this item, enforce stop-after-N-sent.
                        after_mid = getattr(cand, "message_id", None)
                        if (before_mid is None) and after_mid is not None and st2.active_session_id and st2.control_chat_id is not None:
                            s = getattr(st2, "settings", {})
                            s = dict(s) if isinstance(s, dict) else {}
                            try:
                                target = int(s.get("target_sent_per_session", 0) or 0)
                            except Exception:
                                target = 0
                            target = max(0, min(500, int(target)))

                            if target > 0:
                                sid = str(st2.active_session_id)
                                sent_count = 0
                                for vv in (st2.videos or {}).values():
                                    try:
                                        if str(getattr(vv, "session_id", "")) != sid:
                                            continue
                                        if str(getattr(vv, "status", "")) == "deleted":
                                            continue
                                        if getattr(vv, "message_id", None) is None:
                                            continue
                                        if getattr(vv, "message_chat_id", None) != int(st2.control_chat_id):
                                            continue
                                        sent_count += 1
                                    except Exception:
                                        continue

                                if sent_count >= target:
                                    # Stop session cleanly and preserve a meaningful reason.
                                    st2.last_session_stop_reason = f"target_sent_reached:{sent_count}"
                                    st2 = stop_session(deps, st2)
                                    st2.session_paused = False
                                    save_state_locked(cfg.state_file, st2)
                                    try:
                                        _tg_api(
                                            cfg.telegram_token,
                                            "sendMessage",
                                            data={
                                                "chat_id": str(int(st2.control_chat_id)),
                                                "text": f"✅ Objectif atteint: {sent_count}/{target} vidéos envoyées sur Telegram. Session stoppée.",
                                            },
                                        )
                                    except Exception:
                                        pass
                except Exception:
                    # Don't crash the loop, but keep a hint for debugging.
                    print("[TELEGRAM] preview_send_failed")

                # Device loss/lock auto-stop notification (throttled).
                try:
                    now = time.time()
                    if prev_active and not st2.active_session_id and (st2.device_status in {"DISCONNECTED", "LOCKED"}):
                        cooldown_ok = (now - float(st2.last_device_alert_ts or 0.0)) >= float(cfg.risk_alert_cooldown_seconds)
                        if cooldown_ok:
                            _tg_api(
                                cfg.telegram_token,
                                "sendMessage",
                                data={
                                    "chat_id": str(int(st2.control_chat_id)),
                                    "text": f"📵 Session stoppée: device={st2.device_status}",
                                },
                            )
                            st2.last_device_alert_ts = float(now)
                            save_state_locked(cfg.state_file, st2)
                except Exception:
                    pass

                # Risk alerts (throttled) + auto-stop notice.
                try:
                    now = time.time()
                    should_alert = (risk.level in {"WARNING", "HIGH_RISK"})
                    cooldown_ok = (now - float(st2.last_risk_alert_ts or 0.0)) >= float(cfg.risk_alert_cooldown_seconds)
                    try:
                        s = getattr(st2, "settings", {})
                        risk_safety_on = bool((s or {}).get("risk_safety_enabled", True)) if isinstance(s, dict) else True
                    except Exception:
                        risk_safety_on = True

                    safety_hint = ("" if risk_safety_on else " | safety=OFF")

                    if should_alert and (cooldown_ok or (risk.level != prev_level)):
                        _tg_api(
                            cfg.telegram_token,
                            "sendMessage",
                            data={
                                "chat_id": str(int(st2.control_chat_id)),
                                "text": f"⚠️ Risk={risk.level} | {risk.justification} | remaining={int(risk.remaining_seconds)}s{safety_hint}",
                            },
                        )
                        st2.last_risk_alert_ts = float(now)
                        st2.last_risk_level = risk.level
                        save_state_locked(cfg.state_file, st2)

                    if risk_safety_on and risk.level == "HIGH_RISK" and not st2.active_session_id:
                        _tg_api(
                            cfg.telegram_token,
                            "sendMessage",
                            data={
                                "chat_id": str(int(st2.control_chat_id)),
                                "text": "🛑 Session stoppée automatiquement (HIGH_RISK).",
                            },
                        )
                except Exception:
                    pass

                # Allow tuning loop speed from Telegram settings.
                try:
                    s = getattr(st2, "settings", {})
                    if isinstance(s, dict) and "loop_sleep_seconds" in s:
                        sleep_s = float(s.get("loop_sleep_seconds") or cfg.step_sleep_seconds)
                    else:
                        sleep_s = float(cfg.step_sleep_seconds)
                except Exception:
                    sleep_s = float(cfg.step_sleep_seconds)
                sleep_s = max(0.2, min(30.0, float(sleep_s)))
                time.sleep(sleep_s)
            except Exception:
                time.sleep(2.0)

    threading.Thread(target=session_loop, daemon=True).start()

    # Restore persisted update offset.
    st0 = load_state_locked(cfg.state_file)
    offset = int(st0.last_update_id or 0) + 1
    print(f"[V3] polling Telegram getUpdates offset={offset}", flush=True)

    last_heartbeat = float(time.time())

    while True:
        try:
            try:
                now = float(time.time())
                if (now - last_heartbeat) >= 30.0:
                    print(f"[V3] alive offset={offset}", flush=True)
                    last_heartbeat = now
                updates = _tg_api(
                    cfg.telegram_token,
                    "getUpdates",
                    params={"timeout": "50", "offset": str(offset)},
                ) or []
            except RuntimeError as e:
                # Telegram returns 409 when another long-poll is active for the same bot token.
                # This commonly happens if another instance is still running.
                msg = str(e)
                if "'error_code': 409" in msg or "Conflict: terminated by other getUpdates request" in msg:
                    time.sleep(3.0)
                    continue
                raise
            for upd in updates:
                if not isinstance(upd, dict):
                    continue
                uid = int(upd.get("update_id") or 0)
                if uid >= offset:
                    offset = uid + 1

                # Persist offset frequently (crash-safe).
                def _upd(st):
                    st.last_update_id = uid

                update_state_locked(cfg.state_file, _upd)

                msg = upd.get("message") or {}
                cbq = upd.get("callback_query") or {}

                if isinstance(msg, dict) and "text" in msg:
                    chat = msg.get("chat") or {}
                    chat_id = int(chat.get("id") or 0)
                    if not is_allowed(chat_id, allowed=cfg.telegram_allowed_chat_ids):
                        continue
                    text = str(msg.get("text") or "").strip()
                    _log_tg(f"[TG][IN] msg chat_id={chat_id} text={_short(text, 200)!r}")
                    if text.startswith("/start") or text.startswith("/v3"):
                        # 1) Welcome message
                        send_welcome(chat_id)
                        # 2) Menu in a separate message
                        show_home(chat_id, force_new=True)
                        # 3) Upgrade recent already-sent previews so old notifications get the STV button.
                        try:
                            _upgrade_recent_previews_add_stv_button(cfg, chat_id=chat_id, limit=20)
                        except Exception:
                            pass
                        continue

                if isinstance(cbq, dict) and cbq.get("data"):
                    data = str(cbq.get("data") or "")
                    chat = (cbq.get("message") or {}).get("chat") or {}
                    chat_id = int(chat.get("id") or 0)
                    if not is_allowed(chat_id, allowed=cfg.telegram_allowed_chat_ids):
                        continue

                    try:
                        cb_id = str(cbq.get("id") or "").strip()
                        from_u = cbq.get("from") or {}
                        uname = str((from_u.get("username") or from_u.get("first_name") or "") if isinstance(from_u, dict) else "")
                        _log_tg(f"[TG][IN] cb chat_id={chat_id} from={_short(uname, 40)!r} id={_short(cb_id, 40)!r} data={_short(data, 200)!r}")
                    except Exception:
                        _log_tg(f"[TG][IN] cb chat_id={chat_id} data={_short(data, 200)!r}")

                    if data == "v3:home":
                        show_home(chat_id, force_new=True)
                        _log_tg("[TG][OK] action=home")
                        continue

                    def _answer_cbq(text: str) -> None:
                        try:
                            cid = str(cbq.get("id") or "")
                            if cid:
                                _tg_api(
                                    cfg.telegram_token,
                                    "answerCallbackQuery",
                                    data={"callback_query_id": cid, "text": str(text or ""), "show_alert": "false"},
                                )
                        except Exception:
                            pass

                    # Delete-all videos (current)
                    if data == "v3:videos:purge":
                        show_purge_videos_confirm(chat_id)
                        _log_tg("[TG][OK] action=videos_purge_confirm")
                        continue

                    if data == "v3:videos:purge:no":
                        show_home(chat_id, force_new=True)
                        _log_tg("[TG][OK] action=videos_purge_cancel")
                        continue

                    if data == "v3:videos:purge:yes":
                        _purge_all_videos_state(chat_id)
                        show_home(chat_id, force_new=True)
                        _log_tg("[TG][OK] action=videos_purge_exec")
                        continue

                    # Backward compat: old clip-related callbacks now map to video purge.
                    if data == "v3:clips:purge":
                        show_purge_videos_confirm(chat_id)
                        continue
                    if data == "v3:clips:purge:no":
                        show_home(chat_id, force_new=True)
                        continue
                    if data == "v3:clips:purge:yes":
                        _purge_all_videos_state(chat_id)
                        show_home(chat_id, force_new=True)
                        continue

                    if data == "v3:settings":
                        show_settings(chat_id, page="main")
                        _log_tg("[TG][OK] action=settings_main")
                        continue

                    if data == "v3:settings:legacy":
                        show_settings(chat_id, page="legacy")
                        _log_tg("[TG][OK] action=settings_legacy")
                        continue

                    if data == "v3:settings:virality":
                        show_settings(chat_id, page="virality")
                        _log_tg("[TG][OK] action=settings_virality")
                        continue

                    if data == "v3:stv:teach":
                        # Prompt user to start listening for a single TAP on the device.
                        try:
                            kb = {"inline_keyboard": [[{"text": "Démarrer écoute (4s)", "callback_data": "v3:stv:teach:start"}, {"text": "Annuler", "callback_data": "v3:stv:teach:cancel"}]]}
                            _tg_api(cfg.telegram_token, "sendMessage", data={"chat_id": str(chat_id), "text": "📳 Préparez l'écran sur l'appareil Android puis appuyez UNE FOIS sur la zone âge. Ensuite, cliquez 'Démarrer écoute' dans ce chat.", "reply_markup": __import__("json").dumps(kb, ensure_ascii=False)})
                        except Exception:
                            pass
                        continue

                    if data == "v3:stv:teach:cancel":
                        try:
                            _tg_api(cfg.telegram_token, "sendMessage", data={"chat_id": str(chat_id), "text": "Annulé."})
                        except Exception:
                            pass
                        continue

                    if data == "v3:stv:teach:start":
                        # Perform a short adb getevent listen to capture a single touch coordinate.
                        try:
                            _answer_cbq("Écoute en cours…")
                        except Exception:
                            pass

                        try:
                            import subprocess, re, json as _json

                            base = _adb_base(android_agent=deps.android_agent)

                            # Determine screen size
                            screen_w = None
                            screen_h = None
                            try:
                                cp = subprocess.run(base + ["shell", "wm", "size"], capture_output=True, timeout=3.0)
                                out = (cp.stdout or b"").decode("utf-8", errors="replace")
                                m = re.search(r"(\d+)x(\d+)", out)
                                if m:
                                    screen_w = int(m.group(1))
                                    screen_h = int(m.group(2))
                            except Exception:
                                screen_w = None
                                screen_h = None

                            # Run getevent - listen for ~10s
                            # Capture raw getevent output for debugging and try to parse coordinates.
                            proc = subprocess.Popen(base + ["shell", "getevent", "-lt"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                            xs = []
                            ys = []
                            raw_lines: list[str] = []
                            getevent_path = None

                            def _parse_num(tok: str) -> int | None:
                                if not tok:
                                    return None
                                s = str(tok).strip()
                                try:
                                    return int(s, 0)
                                except Exception:
                                    try:
                                        return int(s, 16)
                                    except Exception:
                                        try:
                                            return int(s, 10)
                                        except Exception:
                                            return None

                            # Non-blocking reader: push lines into a queue from a background thread.
                            q: "queue.Queue[str]" = queue.Queue()

                            def _reader() -> None:
                                try:
                                    if proc.stdout is None:
                                        return
                                    for ln in proc.stdout:
                                        try:
                                            q.put(ln)
                                        except Exception:
                                            pass
                                except Exception:
                                    pass

                            thr = threading.Thread(target=_reader, daemon=True)
                            thr.start()

                            end = time.time() + 10.0
                            try:
                                while time.time() < end:
                                    try:
                                        line = q.get(timeout=0.12)
                                    except Exception:
                                        # no line available this cycle
                                        continue
                                    if not line:
                                        continue
                                    # keep raw line for debug
                                    try:
                                        raw_lines.append(line.rstrip("\n"))
                                    except Exception:
                                        pass

                                    # Look for ABS_MT_POSITION_X/Y or ABS_X/ABS_Y (hex or decimal)
                                    m1 = re.search(r"ABS_MT_POSITION_X\s+([0-9a-fA-Fx]+)", line)
                                    m2 = re.search(r"ABS_MT_POSITION_Y\s+([0-9a-fA-Fx]+)", line)
                                    m5 = re.search(r"\bABS_X\s+([0-9a-fA-Fx]+)", line)
                                    m6 = re.search(r"\bABS_Y\s+([0-9a-fA-Fx]+)", line)
                                    if m1:
                                        v = _parse_num(m1.group(1))
                                        if v is not None:
                                            xs.append(v)
                                    if m2:
                                        v = _parse_num(m2.group(1))
                                        if v is not None:
                                            ys.append(v)
                                    if m5:
                                        v = _parse_num(m5.group(1))
                                        if v is not None:
                                            xs.append(v)
                                    if m6:
                                        v = _parse_num(m6.group(1))
                                        if v is not None:
                                            ys.append(v)
                                    m3 = re.search(r"position_x[:=]?\s*([0-9]+)", line, re.IGNORECASE)
                                    m4 = re.search(r"position_y[:=]?\s*([0-9]+)", line, re.IGNORECASE)
                                    if m3:
                                        try:
                                            xs.append(int(m3.group(1)))
                                        except Exception:
                                            pass
                                    if m4:
                                        try:
                                            ys.append(int(m4.group(1)))
                                        except Exception:
                                            pass
                            finally:
                                try:
                                    proc.kill()
                                except Exception:
                                    pass
                                try:
                                    thr.join(timeout=1.0)
                                except Exception:
                                    pass
                                # Persist raw getevent output for debugging
                                try:
                                    out_dir = Path("storage/v3")
                                    out_dir.mkdir(parents=True, exist_ok=True)
                                    getevent_path = out_dir / f"stv_getevent_{int(time.time())}.log"
                                    getevent_path.write_text("\n".join(raw_lines), encoding="utf-8")
                                except Exception:
                                    getevent_path = None

                            # median-ish value
                            if not xs or not ys:
                                # If no touch coords parsed, send a helpful message and an extract of the raw getevent log.
                                msg_text = "Aucune touche détectée. Vérifiez que l'appareil est connecté et que vous avez appuyé sur l'écran."
                                try:
                                    if getevent_path is not None and getevent_path.exists():
                                        snippet = getevent_path.read_text(encoding="utf-8")[:1500]
                                        msg_text += f"\n\nLog getevent enregistré: {getevent_path.as_posix()}\n\nExtrait:\n{snippet}"
                                except Exception:
                                    pass
                                _tg_api(cfg.telegram_token, "sendMessage", data={"chat_id": str(chat_id), "text": msg_text})
                                # continue to next iteration of outer handler loop
                                continue

                            # median-ish value

                            # median-ish value
                            x = int(sorted(xs)[len(xs) // 2])
                            y = int(sorted(ys)[len(ys) // 2])
                            x_ratio = None
                            y_ratio = None
                            try:
                                if screen_w and screen_h:
                                    x_ratio = float(x) / float(screen_w)
                                    y_ratio = float(y) / float(screen_h)
                            except Exception:
                                x_ratio = None
                                y_ratio = None

                            data_out = {
                                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "adb": str((deps.android_agent._cfg.adb_path) if getattr(deps, 'android_agent', None) else os.getenv('V3_ADB_PATH', '')),
                                "screen_w": screen_w,
                                "screen_h": screen_h,
                                "x_px": x,
                                "y_px": y,
                                "x_ratio": x_ratio,
                                "y_ratio": y_ratio,
                                "note": "learned via TG teach",
                            }
                            p = Path("storage/v3")
                            p.mkdir(parents=True, exist_ok=True)
                            outp = p / "stv_click.json"
                            outp.write_text(_json.dumps(data_out, indent=2), encoding="utf-8")

                            # Log to terminal for debug
                            print(f"[STV][TEACH] saved_click x={x} y={y} screen={screen_w}x{screen_h} ratio=({x_ratio},{y_ratio}) path={outp.as_posix()}", flush=True)

                            # Prepare message including getevent log path and an excerpt for debugging
                            msg = f"✅ Position enregistrée: x={x} y={y} (ratio={x_ratio},{y_ratio})\nFichier: {outp.as_posix()}"
                            try:
                                if getevent_path is not None and getevent_path.exists():
                                    snippet = getevent_path.read_text(encoding="utf-8")[:1500]
                                    msg += f"\n\nLog getevent: {getevent_path.as_posix()}\n\nExtrait:\n{snippet}"
                            except Exception:
                                pass

                            _tg_api(cfg.telegram_token, "sendMessage", data={"chat_id": str(chat_id), "text": msg})
                        except Exception as e:
                            try:
                                _tg_api(cfg.telegram_token, "sendMessage", data={"chat_id": str(chat_id), "text": f"Erreur pendant l'écoute: {type(e).__name__}: {e}"})
                            except Exception:
                                pass
                        continue

                    if data == "v3:settings:reset":
                        st = load_state_locked(cfg.state_file)
                        st.settings = {}
                        st.last_session_stop_reason = None
                        save_state_locked(cfg.state_file, st)
                        show_settings(chat_id, force_new=True, page="main")
                        _log_tg("[TG][OK] action=settings_reset")
                        continue

                    if data == "v3:toggle:device_input":
                        st = load_state_locked(cfg.state_file)
                        _ensure_settings_defaults(st)
                        s = dict(getattr(st, "settings", {}) or {})
                        cur = bool(s.get("device_input_enabled", False))
                        s["device_input_enabled"] = (not cur)
                        st.settings = s
                        save_state_locked(cfg.state_file, st)
                        show_settings(chat_id, force_new=True, page="main")
                        _log_tg(f"[TG][OK] action=toggle_device_input value={s.get('device_input_enabled')}")
                        continue

                    if data == "v3:toggle:risk_safety":
                        st = load_state_locked(cfg.state_file)
                        _ensure_settings_defaults(st)
                        s = dict(getattr(st, "settings", {}) or {})
                        cur = bool(s.get("risk_safety_enabled", True))
                        s["risk_safety_enabled"] = (not cur)
                        st.settings = s
                        save_state_locked(cfg.state_file, st)
                        show_settings(chat_id, force_new=True, page="main")
                        _log_tg(f"[TG][OK] action=toggle_risk_safety value={s.get('risk_safety_enabled')}")
                        continue

                    if data.startswith("v3:set:"):
                        # Format: v3:set:<key>:<dir>
                        parts = data.split(":")
                        if len(parts) >= 4:
                            k = parts[2]
                            direction = parts[3]
                            # Map short keys to state settings keys.
                            key_map = {
                                "thr": "score_threshold",
                                "wb": "weight_banalite",
                                "wp": "weight_potentiel_viral",
                                "wr": "weight_rythme",
                                "rt": "rythme_target",

                                "tv": "threshold_viral",
                                "vwb": "viral_w_banalite",
                                "vwp": "viral_w_potentiel_viral",
                                "vwr": "viral_w_rythme",
                                "vrt": "viral_rythme_target",

                                "tl": "threshold_latent",
                                "lwb": "latent_w_banalite",
                                "lwp": "latent_w_potentiel_viral",
                                "lwr": "latent_w_rythme",
                                "lrt": "latent_rythme_target",

                                "pause": "scroll_pause_seconds",
                                "sleep": "loop_sleep_seconds",
                                "tgt": "target_sent_per_session",

                                # STV / OCR tuning
                                "ocr": "stv_ocr_tries",
                                "rx0": "stv_right_crop_x0_ratio",
                                "ry0": "stv_right_crop_y0_ratio",
                                "ry1": "stv_right_crop_y1_ratio",
                                "vr": "stv_max_views_like_ratio",
                                "vm": "stv_abs_max_views",
                            }
                            full_key = key_map.get(k)
                            if full_key:
                                sign = -1.0 if direction == "-" else 1.0
                                step = 0.0
                                if k in {"thr", "wb", "wp", "wr", "rt", "tv", "vwb", "vwp", "vwr", "vrt", "tl", "lwb", "lwp", "lwr", "lrt"}:
                                    step = 0.02
                                elif k in {"pause"}:
                                    step = 0.2
                                elif k in {"sleep"}:
                                    step = 0.5
                                elif k in {"tgt"}:
                                    step = 1.0
                                elif k in {"ocr"}:
                                    step = 1.0
                                elif k in {"rx0", "ry0", "ry1"}:
                                    step = 0.01
                                elif k in {"vr"}:
                                    step = 50.0
                                elif k in {"vm"}:
                                    step = 10_000_000.0
                                _apply_setting_delta(chat_id, full_key, step * sign)
                        # Keep user on the relevant settings page after the change.
                        if k in {"thr", "wb", "wp", "wr", "rt"}:
                            show_settings(chat_id, page="legacy")
                        elif k in {"tv", "vwb", "vwp", "vwr", "vrt", "tl", "lwb", "lwp", "lwr", "lrt", "ocr", "rx0", "ry0", "ry1", "vr", "vm"}:
                            show_settings(chat_id, page="virality")
                        else:
                            show_settings(chat_id, page="main")
                        continue

                    if data == "v3:sess:start":
                        st = load_state_locked(cfg.state_file)
                        if _ensure_settings_defaults(st):
                            save_state_locked(cfg.state_file, st)

                        # Explicit V3 trace (helps detect wrong instance/legacy).
                        try:
                            s = dict(getattr(st, "settings", {}) or {})
                            adb_on = bool(s.get("device_input_enabled", False))
                        except Exception:
                            adb_on = False
                        print(f"[V3] UI start_session chat_id={chat_id} build={build_id} pid={proc_id} adb_input={'ON' if adb_on else 'OFF'}", flush=True)

                        st = start_session(deps, st, chat_id)
                        st.session_paused = False
                        save_state_locked(cfg.state_file, st)
                        if not st.active_session_id and st.device_status and st.device_status != "READY":
                            extra = ""
                            try:
                                if deps.android_agent is not None and not deps.android_agent.adb_available():
                                    extra = "\n⚠️ adb introuvable: installe Android Platform Tools ou définis V3_ADB_PATH (chemin vers adb.exe)."
                            except Exception:
                                extra = ""
                            _tg_api(
                                cfg.telegram_token,
                                "sendMessage",
                                data={"chat_id": str(chat_id), "text": f"⛔ Démarrage refusé: device={st.device_status}{extra}"},
                            )
                        else:
                            # Success path acknowledgement.
                            _tg_api(
                                cfg.telegram_token,
                                "sendMessage",
                                data={
                                    "chat_id": str(chat_id),
                                    "text": (
                                        "✅ V3 session démarrée\n"
                                        + f"Build: {build_id} | PID: {proc_id}\n"
                                        + f"Session: {st.active_session_id or '-'}\n"
                                        + f"ADB input (setting): {'ON' if adb_on else 'OFF'}"
                                    ),
                                },
                            )
                        show_home(chat_id)
                        _log_tg("[TG][OK] action=session_start")
                        continue

                    if data == "v3:sess:pause":
                        st = load_state_locked(cfg.state_file)
                        st.session_paused = True
                        save_state_locked(cfg.state_file, st)
                        show_home(chat_id)
                        _log_tg("[TG][OK] action=session_pause")
                        continue

                    if data == "v3:sess:resume":
                        st = load_state_locked(cfg.state_file)
                        st.session_paused = False
                        save_state_locked(cfg.state_file, st)
                        show_home(chat_id)
                        _log_tg("[TG][OK] action=session_resume")
                        continue

                    if data == "v3:sess:stop":
                        st = load_state_locked(cfg.state_file)
                        st = stop_session(deps, st)
                        st.session_paused = False
                        save_state_locked(cfg.state_file, st)
                        show_home(chat_id)
                        _log_tg("[TG][OK] action=session_stop")
                        continue

                    if data == "v3:sess:step":
                        print(f"[V3] UI step_once chat_id={chat_id} build={build_id} pid={proc_id}", flush=True)
                        _, item, risk = step_once(deps)
                        try:
                            _log_tg(
                                f"[TG][OK] action=session_step item={(getattr(item, 'internal_id', None) if item is not None else None)} risk={getattr(risk, 'level', None)}"
                            )
                        except Exception:
                            _log_tg("[TG][OK] action=session_step")
                        if item is not None:
                            show_item(chat_id, item.internal_id)
                        else:
                            show_home(chat_id)
                        continue

                    if data.startswith("v3:list:"):
                        parts = data.split(":")
                        which = parts[2] if len(parts) >= 3 else "pending"
                        page = 0
                        if len(parts) >= 4:
                            try:
                                page = int(parts[3])
                            except Exception:
                                page = 0
                        show_list(chat_id, which, page=page)
                        continue

                    if data.startswith("v3:item:open:"):
                        vid = data.split(":")[-1]
                        try:
                            st = load_state_locked(cfg.state_file)
                            v = st.videos.get(vid)
                            if v is not None:
                                _send_preview_to_chat(cfg, chat_id, v, force=True)
                                save_state_locked(cfg.state_file, st)
                        except Exception:
                            pass
                        show_item(chat_id, vid)
                        continue

                    if data.startswith("v3:item:stv:"):
                        vid = data.split(":")[-1]
                        msg_obj = cbq.get("message") or {}
                        message_id = msg_obj.get("message_id")
                        try:
                            message_id_i = int(message_id) if message_id is not None else None
                        except Exception:
                            message_id_i = None

                        print(f"[STV] tg_button clicked vid={vid} chat_id={chat_id} message_id={message_id_i}", flush=True)

                        st = load_state_locked(cfg.state_file)
                        if _ensure_settings_defaults(st):
                            save_state_locked(cfg.state_file, st)
                        v = st.videos.get(vid)
                        if v is None:
                            _answer_cbq("Item introuvable")
                            continue

                        # URL source resolution:
                        # 1) try to extract the URL actually present in the Telegram message (most reliable for user-visible notif)
                        # 2) fallback to stored meta.clipboard_url
                        # 3) fallback to v.source_url
                        import re

                        meta = getattr(v, "meta", None)
                        # message text/caption (may contain the URL shown in Telegram)
                        old_msg_text = ""
                        try:
                            old_msg_text = str((msg_obj.get("text") or msg_obj.get("caption") or "") or "")
                        except Exception:
                            old_msg_text = ""
                        url_from_msg = ""
                        try:
                            m = re.search(r"(https?://\S+)", old_msg_text)
                            if m:
                                url_from_msg = m.group(1).strip()
                        except Exception:
                            url_from_msg = ""

                        url_clip = ""
                        try:
                            if isinstance(meta, dict):
                                url_clip = str(meta.get("clipboard_url") or "").strip()
                        except Exception:
                            url_clip = ""

                        url_src = str(getattr(v, "source_url", "") or "").strip()

                        # choose first non-empty in order: url_from_msg, url_clip, url_src
                        url = url_from_msg or url_clip or url_src
                        try:
                            print(f"[AGE][DBG] STV resolved URLs msg={url_from_msg!r} meta.clipboard={url_clip!r} stored={url_src!r} chosen={url!r}", flush=True)
                        except Exception:
                            pass

                        if not url:
                            print(f"[STV] no_url vid={vid}", flush=True)
                            _answer_cbq("Pas d’URL")
                            continue

                        _answer_cbq("Calcul STV…")

                        try:
                            from ..stv_refresh import refresh_stv_from_url, strip_existing_stv_block
                            from ..stv_age_api import fetch_created_time

                            # First try the unified created-time extractor (Selenium -> HTML).
                            try:
                                try:
                                    print(f"[AGE][DBG] STV handler pre-fetch vid={vid} source_url={getattr(v,'source_url',None)!r} meta_keys={list((getattr(v,'meta',{}) or {}).keys())} meta_reel_age={(getattr(v,'meta',{}) or {}).get('reel_age_seconds')}", flush=True)
                                except Exception:
                                    pass
                            except Exception:
                                pass

                            age_res = None
                            try:
                                print(f"[AGE][DBG] calling fetch_created_time for url={url}", flush=True)
                                age_res = fetch_created_time(url)
                                print(f"[AGE][DBG] fetch_created_time returned: {age_res}", flush=True)
                            except Exception as e:
                                age_res = None
                                try:
                                    print(f"[AGE][DBG] fetch_created_time exception: {type(e).__name__}: {e}", flush=True)
                                except Exception:
                                    pass

                            try:
                                _log_tg(f"[AGE][STV] vid={vid} url={url} age_res={age_res}")
                            except Exception:
                                pass

                            if age_res is not None:
                                # Build a full temporal analysis using the discovered age.
                                try:
                                    from ..temporal_analysis import analyze_from_meta, format_telegram_block

                                    meta2 = dict(getattr(v, "meta", {}) or {})
                                    # Inject AGE_SECONDS token so parse_relative_pub_time honours the external age.
                                    raw = str(meta2.get("ocr_raw_text") or "")
                                    token = f"AGE_SECONDS = {int(age_res.age_seconds or 0)}"
                                    if token not in raw:
                                        if raw.strip():
                                            meta2["ocr_raw_text"] = raw + "\n\n" + token
                                        else:
                                            meta2["ocr_raw_text"] = token

                                    # Try to enrich meta2 with engagement metrics parsed from the HTML
                                    try:
                                        from ..stv_age_api import try_fetch_metrics_from_html
                                        import urllib.request

                                        html = None
                                        try:
                                            req = urllib.request.Request(str(url), headers={"User-Agent": "snap-bot/1.0"})
                                            with urllib.request.urlopen(req, timeout=5.0) as fh:
                                                raw = fh.read() or b""
                                            html = raw.decode("utf-8", errors="replace")
                                        except Exception:
                                            html = None

                                        if html:
                                            metrics = try_fetch_metrics_from_html(html)
                                            if isinstance(metrics, dict) and metrics:
                                                meta2["ocr_metrics"] = metrics
                                                # also set legacy individual keys
                                                if "likes" in metrics:
                                                    meta2["ocr_likes"] = metrics.get("likes")
                                                if "comments" in metrics:
                                                    meta2["ocr_comments"] = metrics.get("comments")
                                                if "views" in metrics:
                                                    meta2["ocr_views"] = metrics.get("views")
                                    except Exception:
                                        pass

                                    # If no metrics were found in HTML, fall back to the heavy OCR refresh to obtain them.
                                    if not isinstance(meta2.get("ocr_metrics"), dict) or not meta2.get("ocr_metrics"):
                                        try:
                                            s_now = dict(getattr(st, "settings", {}) or {})
                                            refresh_res = refresh_stv_from_url(v, url, android_agent=deps.android_agent, settings=s_now)
                                            # If refresh produced OCR metrics, merge them
                                            if getattr(refresh_res, "raw_ocr_text", None):
                                                meta2["ocr_raw_text"] = (meta2.get("ocr_raw_text") or "") + "\n\n" + getattr(refresh_res, "raw_ocr_text")
                                            # attempt to parse metrics from the refresh debug if provided
                                            try:
                                                dbg = getattr(refresh_res, "debug", "") or ""
                                                # no-op here; analyze_from_meta will parse ocr_raw_text if present
                                            except Exception:
                                                pass
                                        except Exception:
                                            pass

                                    analysis = analyze_from_meta(meta=meta2)
                                    block = format_telegram_block(analysis)
                                    # Ensure absolute date is present when we have an external age
                                    try:
                                        if age_res and getattr(age_res, 'age_seconds', None) is not None:
                                            from datetime import datetime, timezone, timedelta

                                            dt_pub = datetime.now(timezone.utc) - timedelta(seconds=int(age_res.age_seconds))
                                            date_str = dt_pub.astimezone(timezone.utc).strftime("%d/%m/%Y")
                                            # insert date into the published line if missing
                                            try:
                                                ls = [ln for ln in block.splitlines()]
                                                for i_l, ln in enumerate(ls):
                                                    if ln.strip().startswith("📅 Publiée"):
                                                        if date_str not in ln:
                                                            ls[i_l] = ln + f" — {date_str}"
                                                            block = "\n".join(ls)
                                                        break
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass

                                    class _SimpleRes:
                                        pass

                                    res = _SimpleRes()
                                    res.ok = True
                                    res.telegram_block = block
                                    res.debug = "created_time_full_analysis"
                                except Exception:
                                    # Fallback to the simple block if analysis fails.
                                    s = int(age_res.age_seconds or 0)
                                    if s < 60:
                                        human = f"{s}s"
                                    elif s < 3600:
                                        human = f"{s//60}m"
                                    elif s < 86400:
                                        human = f"{s//3600}h"
                                    else:
                                        human = f"{s//86400}d"
                                    block = f"📅 Publiée : ~{human} (source={age_res.source})"
                                    try:
                                        from datetime import datetime, timezone, timedelta
                                        dt_pub = datetime.now(timezone.utc) - timedelta(seconds=int(age_res.age_seconds or 0))
                                        date_str = dt_pub.astimezone(timezone.utc).strftime("%d/%m/%Y")
                                        block = block + f" — {date_str}"
                                    except Exception:
                                        pass
                                    class _SimpleRes2:
                                        pass

                                    res = _SimpleRes2()
                                    res.ok = True
                                    res.telegram_block = block
                                    res.debug = "created_time_only_fallback"
                            else:
                                # Use the existing STV refresh (OCR/ADB) when created-time isn't found.
                                s_now = dict(getattr(st, "settings", {}) or {})
                                # refresh_stv_from_url expects (video, url, ...)
                                try:
                                    res = refresh_stv_from_url(v, url, android_agent=deps.android_agent, settings=s_now)
                                except Exception as e:
                                    try:
                                        print(f"[AGE][REFRESH] refresh_stv_from_url exception url={url} err={type(e).__name__}:{e}", flush=True)
                                    except Exception:
                                        pass
                                    try:
                                        from ..stv_refresh import StvRefreshResult

                                        res = StvRefreshResult(False, telegram_block="", debug=f"exception:{type(e).__name__}:{e}")
                                    except Exception:
                                        class _FallbackRes:
                                            pass

                                        res = _FallbackRes()
                                        res.ok = False
                                        res.telegram_block = ""
                                        res.debug = f"exception:{type(e).__name__}:{e}"

                                # If OCR was unavailable during refresh, try a plain-HTML fallback
                                # to at least recover a creation time without OCR/Tesseract.
                                try:
                                    # Defensive: ensure `res` is not None and has expected attributes.
                                    if res is None:
                                        try:
                                            print(f"[AGE][REFRESH] warning: res is None after refresh for url={url}", flush=True)
                                        except Exception:
                                            pass
                                        try:
                                            from ..stv_refresh import StvRefreshResult

                                            res = StvRefreshResult(False, telegram_block="", debug="res_none_after_refresh")
                                        except Exception:
                                            class _FallbackRes2:
                                                pass

                                            res = _FallbackRes2()
                                            res.ok = False
                                            res.telegram_block = ""
                                            res.debug = "res_none_after_refresh"

                                    tb = str(getattr(res, "telegram_block", "") or "")
                                    debug_s = str(getattr(res, "debug", "") or "")

                                    if (not bool(getattr(res, "ok", False))) and (
                                        "OCR indisponible" in tb or "OCR indisponible" in debug_s or "ocr" in debug_s.lower()
                                    ):
                                        try:
                                            from ..stv_age_api import try_fetch_age_from_html

                                            alt = try_fetch_age_from_html(url)
                                            if alt is not None:
                                                s2 = int(alt.age_seconds or 0)
                                                if s2 < 60:
                                                    human2 = f"{s2}s"
                                                elif s2 < 3600:
                                                    human2 = f"{s2//60}m"
                                                elif s2 < 86400:
                                                    human2 = f"{s2//3600}h"
                                                else:
                                                    human2 = f"{s2//86400}d"
                                                block2 = f"📅 Publiée : ~{human2} (source={alt.source})"
                                                class _SimpleRes2:
                                                    pass

                                                res = _SimpleRes2()
                                                res.ok = True
                                                res.telegram_block = block2
                                                res.debug = (getattr(res, "debug", "") or "") + ";html_fallback_after_ocr"
                                        except Exception:
                                            pass
                                except Exception:
                                    pass

                            # Rebuild message text by appending refreshed block (no storage change).
                            # Prefer authoritative caption from storage (`_caption_for(v)`) so we keep
                            # all metadata and formatting. Fallback to the message object if needed.
                            try:
                                base_text = strip_existing_stv_block(_caption_for(v))
                            except Exception:
                                old_text = ""
                                if isinstance(msg_obj, dict):
                                    old_text = str(msg_obj.get("text") or msg_obj.get("caption") or "")
                                base_text = strip_existing_stv_block(old_text)
                            new_text = (base_text + "\n\n" + str(res.telegram_block or "").strip()).strip()

                            kb = _preview_keyboard(vid)

                            def _is_not_modified(exc: Exception) -> bool:
                                return "message is not modified" in str(exc).lower()

                            if message_id_i is None:
                                _tg_api(
                                    cfg.telegram_token,
                                    "sendMessage",
                                    data={
                                        "chat_id": str(int(chat_id)),
                                        "text": new_text,
                                        "disable_web_page_preview": "false",
                                        "reply_markup": json.dumps(kb, ensure_ascii=False),
                                    },
                                )
                            else:
                                edited = False
                                try:
                                    _tg_api(
                                        cfg.telegram_token,
                                        "editMessageText",
                                        data={
                                            "chat_id": str(int(chat_id)),
                                            "message_id": str(int(message_id_i)),
                                            "text": new_text,
                                            "disable_web_page_preview": "false",
                                            "reply_markup": json.dumps(kb, ensure_ascii=False),
                                        },
                                    )
                                    edited = True
                                except Exception as e_text:
                                    if _is_not_modified(e_text):
                                        edited = True
                                    else:
                                        try:
                                            _tg_api(
                                                cfg.telegram_token,
                                                "editMessageCaption",
                                                data={
                                                    "chat_id": str(int(chat_id)),
                                                    "message_id": str(int(message_id_i)),
                                                    "caption": new_text,
                                                    "reply_markup": json.dumps(kb, ensure_ascii=False),
                                                },
                                            )
                                            edited = True
                                        except Exception as e_cap:
                                            if _is_not_modified(e_cap):
                                                edited = True

                                if not edited:
                                    _tg_api(
                                        cfg.telegram_token,
                                        "sendMessage",
                                        data={
                                            "chat_id": str(int(chat_id)),
                                            "text": new_text,
                                            "disable_web_page_preview": "false",
                                            "reply_markup": json.dumps(kb, ensure_ascii=False),
                                        },
                                    )

                            print(f"[STV] tg_refresh_result vid={vid} ok={res.ok} debug={res.debug}", flush=True)
                            _answer_cbq("STV mis à jour" if res.ok else "STV: échec (voir logs)")
                        except Exception as e:
                            print(f"[STV] tg_refresh_exception vid={vid} {type(e).__name__}: {e}", flush=True)
                            _answer_cbq("STV: erreur (voir logs)")
                        continue

                    if data.startswith("v3:item:approve:"):
                        vid = data.split(":")[-1]
                        set_status(deps, vid, "approved")
                        _log_tg(f"[TG][OK] action=item_approve vid={vid}")
                        try:
                            st = load_state_locked(cfg.state_file)
                            v = st.videos.get(vid)
                            if v is not None:
                                _try_edit_preview(cfg, v)
                        except Exception:
                            pass
                        show_item(chat_id, vid)
                        continue

                    if data.startswith("v3:item:reject:"):
                        vid = data.split(":")[-1]
                        set_status(deps, vid, "rejected")
                        _log_tg(f"[TG][OK] action=item_reject vid={vid}")
                        try:
                            st = load_state_locked(cfg.state_file)
                            v = st.videos.get(vid)
                            if v is not None:
                                _try_edit_preview(cfg, v)
                        except Exception:
                            pass
                        show_item(chat_id, vid)
                        continue

                    if data.startswith("v3:item:delete:"):
                        vid = data.split(":")[-1]
                        try:
                            st = load_state_locked(cfg.state_file)
                            v = st.videos.get(vid)
                            if v is not None:
                                v.status = "deleted"  # type: ignore[assignment]
                                save_state_locked(cfg.state_file, st)
                                _try_edit_preview(cfg, v)
                            else:
                                set_status(deps, vid, "deleted")
                        except Exception:
                            set_status(deps, vid, "deleted")
                        show_home(chat_id)
                        _log_tg(f"[TG][OK] action=item_delete vid={vid}")
                        continue

                    if data.startswith("v3:item:like:"):
                        vid = data.split(":")[-1]
                        _, ok = like_if_approved(deps, vid)
                        if ok:
                            _tg_api(cfg.telegram_token, "sendMessage", data={"chat_id": str(chat_id), "text": "❤️ Like simulé enregistré."})
                        else:
                            _tg_api(cfg.telegram_token, "sendMessage", data={"chat_id": str(chat_id), "text": "Like refusé: item non approuvé."})
                        show_item(chat_id, vid)
                        _log_tg(f"[TG][OK] action=item_like vid={vid} ok={ok}")
                        continue

        except KeyboardInterrupt:
            print("[V3] stopped (KeyboardInterrupt)", flush=True)
            return
        except Exception:
            time.sleep(1.5)
