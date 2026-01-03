from __future__ import annotations

import time
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import requests

from bot.formats.anomalie_objet.env import load_anomalie_objet_dotenv
from bot.formats.anomalie_objet.pipeline import AnomalieObjetResult, generate_one_anomalie_objet


STATE_LOCK = threading.Lock()
STATE_FILE = Path(__file__).resolve().parents[3] / "storage" / "formats" / "anomalie_objet" / "telegram_state.json"


def _log(prefix: str, msg: str) -> None:
    print(f"[{prefix}] {msg}")


@dataclass
class Clip:
    clip_id: str
    video_path: str
    object_name: str
    anomaly: str
    factual_text: str | None
    status: str = "pending"  # pending | approved | rejected
    message_chat_id: int | None = None
    message_id: int | None = None


@dataclass
class ChatState:
    clips: dict[str, Clip] = field(default_factory=dict)
    ordered_ids: list[str] = field(default_factory=list)  # pending order
    approved_ids: list[str] = field(default_factory=list)
    rejected_ids: list[str] = field(default_factory=list)

    generating: bool = False
    control_message_id: int | None = None
    active_menu: str = "main"  # main | queue | approved | rejected

    stage: str = ""
    detail: str = ""
    last_ui_ts: float = 0.0


_CHAT: dict[int, ChatState] = {}


def _ensure_storage_dir() -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _state(chat_id: int) -> ChatState:
    if chat_id not in _CHAT:
        _CHAT[chat_id] = ChatState()
        _save_state_locked()
    return _CHAT[chat_id]


def _clip_to_dict(c: Clip) -> dict:
    return {
        "clip_id": c.clip_id,
        "video_path": c.video_path,
        "object_name": c.object_name,
        "anomaly": c.anomaly,
        "factual_text": c.factual_text,
        "status": c.status,
        "message_chat_id": c.message_chat_id,
        "message_id": c.message_id,
    }


def _dict_to_clip(d: dict) -> Clip:
    return Clip(
        clip_id=str(d.get("clip_id") or ""),
        video_path=str(d.get("video_path") or ""),
        object_name=str(d.get("object_name") or ""),
        anomaly=str(d.get("anomaly") or ""),
        factual_text=(str(d.get("factual_text")) if d.get("factual_text") is not None else None),
        status=str(d.get("status") or "pending"),
        message_chat_id=(int(d.get("message_chat_id")) if d.get("message_chat_id") is not None else None),
        message_id=(int(d.get("message_id")) if d.get("message_id") is not None else None),
    )


def _save_state() -> None:
    _ensure_storage_dir()
    try:
        import json

        payload = {
            "chats": {
                str(cid): {
                    "clips": {k: _clip_to_dict(v) for k, v in st.clips.items()},
                    "ordered_ids": list(st.ordered_ids),
                    "approved_ids": list(st.approved_ids),
                    "rejected_ids": list(st.rejected_ids),
                    "generating": False,
                    "control_message_id": st.control_message_id,
                    "active_menu": st.active_menu,
                }
                for cid, st in _CHAT.items()
            }
        }
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception:
        return


def _save_state_locked() -> None:
    with STATE_LOCK:
        _save_state()


def _load_state() -> None:
    _ensure_storage_dir()
    if not STATE_FILE.exists():
        return
    try:
        import json

        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        chats = payload.get("chats") or {}
        if not isinstance(chats, dict):
            return
        for cid_s, st_d in chats.items():
            try:
                cid = int(cid_s)
            except Exception:
                continue
            if not isinstance(st_d, dict):
                continue
            st = ChatState()
            st.control_message_id = st_d.get("control_message_id")
            st.active_menu = str(st_d.get("active_menu") or "main")
            clips = st_d.get("clips") or {}
            if isinstance(clips, dict):
                for k, v in clips.items():
                    if isinstance(v, dict):
                        c = _dict_to_clip(v)
                        if c.clip_id:
                            st.clips[c.clip_id] = c
            st.ordered_ids = [str(x) for x in (st_d.get("ordered_ids") or []) if str(x).strip()]
            st.approved_ids = [str(x) for x in (st_d.get("approved_ids") or []) if str(x).strip()]
            st.rejected_ids = [str(x) for x in (st_d.get("rejected_ids") or []) if str(x).strip()]
            _CHAT[cid] = st
    except Exception:
        return


def _tg_api(method: str, *, params=None, data=None, files=None, timeout=60):
    token = str(__import__("os").environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing (expected in .env.anomalie_objet or environment)")

    url = f"https://api.telegram.org/bot{token}/{method}"
    resp = requests.post(url, params=params, data=data, files=files, timeout=timeout)
    try:
        payload = resp.json()
    except Exception:
        payload = {"ok": False, "description": resp.text}
    if not payload.get("ok"):
        desc = str(payload.get("description") or "")
        code = payload.get("error_code")
        if code == 400 and "message is not modified" in desc.lower() and method.startswith("editMessage"):
            return None
        raise RuntimeError(f"Telegram API error calling {method}: {payload}")
    return payload.get("result")


def _kb(rows):
    return {"inline_keyboard": rows}


def _btn(text: str, cb: str):
    return {"text": text, "callback_data": cb}


def _json(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


def _send_or_edit_panel(chat_id: int, text: str, reply_markup: dict, *, force_new: bool = False):
    st = _state(chat_id)
    if force_new:
        st.control_message_id = None
        _save_state_locked()

    if st.control_message_id is None:
        r = _tg_api(
            "sendMessage",
            data={"chat_id": str(chat_id), "text": text, "reply_markup": _json(reply_markup)},
        )
        if r and isinstance(r, dict) and "message_id" in r:
            st.control_message_id = int(r["message_id"])
            _save_state_locked()
        return

    try:
        _tg_api(
            "editMessageText",
            data={
                "chat_id": str(chat_id),
                "message_id": str(st.control_message_id),
                "text": text,
                "reply_markup": _json(reply_markup),
            },
        )
    except Exception:
        st.control_message_id = None
        _save_state_locked()
        _send_or_edit_panel(chat_id, text, reply_markup)


def _pending_ids(st: ChatState) -> list[str]:
    return [cid for cid in st.ordered_ids if cid in st.clips and st.clips[cid].status == "pending"]


def _clip_caption(clip: Clip) -> str:
    t = clip.factual_text.strip() if isinstance(clip.factual_text, str) and clip.factual_text.strip() else "(no text)"
    return f"Objet: {clip.object_name}\nAnomalie: {clip.anomaly}\nTexte: {t}\n\nStatus: {clip.status}"


def _clip_actions_kb(clip_id: str):
    return _kb(
        [
            [_btn("✅ Approve", f"clip:approve:{clip_id}"), _btn("❌ Reject", f"clip:reject:{clip_id}")],
            [_btn("⬅️ Back", "menu:main")],
        ]
    )


def _send_clip(chat_id: int, clip: Clip):
    caption = _clip_caption(clip)
    with open(clip.video_path, "rb") as f:
        r = _tg_api(
            "sendVideo",
            data={"chat_id": str(chat_id), "caption": caption, "reply_markup": _json(_clip_actions_kb(clip.clip_id))},
            files={"video": f},
            timeout=300,
        )
    clip.message_chat_id = int(r["chat"]["id"])
    clip.message_id = int(r["message_id"])
    _save_state_locked()


def _format_status(st: ChatState) -> str:
    text = "Anomalie visuelle – Objet\n\n"
    if st.generating:
        text += "Status: generating\n"
        if st.stage:
            text += f"Step: {st.stage}\n"
        if st.detail:
            d = st.detail.replace("\n", " ").strip()
            if len(d) > 160:
                d = d[:157] + "…"
            text += f"Info: {d}\n"
        text += "\nUpdates: Pick → Image → Video → Send"
    else:
        q = len(_pending_ids(st))
        a = len(st.approved_ids)
        r = len(st.rejected_ids)
        text += f"Queue: {q} | Approved: {a} | Rejected: {r}"
    return text


def _main_menu(chat_id: int, *, force_new: bool = False):
    st = _state(chat_id)
    st.active_menu = "main"
    _save_state_locked()

    reply = _kb(
        [
            [_btn("▶️ Launch generation", "menu:launch")],
            [_btn("📋 Queue", "menu:queue")],
            [_btn("✅ Approved", "menu:approved")],
            [_btn("❌ Rejected", "menu:rejected")],
        ]
    )
    _send_or_edit_panel(chat_id, _format_status(st), reply, force_new=force_new)


def _list_menu(chat_id: int, which: str):
    st = _state(chat_id)
    st.active_menu = which
    _save_state_locked()

    if which == "queue":
        ids = _pending_ids(st)
        title = "Queue"
    elif which == "approved":
        ids = [cid for cid in st.approved_ids if cid in st.clips]
        title = "Approved"
    else:
        ids = [cid for cid in st.rejected_ids if cid in st.clips]
        title = "Rejected"

    lines = [f"{title}", f"Count: {len(ids)}", ""]
    shown = ids[:10]
    for i, cid in enumerate(shown, start=1):
        c = st.clips.get(cid)
        if not c:
            continue
        lines.append(f"{i}. {c.object_name} ({c.status})")

    rows = []
    for cid in shown:
        rows.append([_btn("🎬 Preview", f"clip:open:{cid}"), _btn("✅", f"clip:approve:{cid}"), _btn("❌", f"clip:reject:{cid}")])

    rows.append([_btn("⬅️ Back", "menu:main")])
    _send_or_edit_panel(chat_id, "\n".join(lines), _kb(rows))


def _set_status(chat_id: int, *, stage: str | None = None, detail: str | None = None, force: bool = False):
    st = _state(chat_id)
    if stage is not None:
        st.stage = stage
    if detail is not None:
        st.detail = detail

    now = time.time()
    if not force and (now - float(st.last_ui_ts or 0.0)) < 1.0:
        return
    st.last_ui_ts = now

    try:
        _main_menu(chat_id)
    except Exception:
        pass


def _generate_thread(chat_id: int):
    st = _state(chat_id)
    st.generating = True
    st.stage = "Starting"
    st.detail = "Preparing"
    st.last_ui_ts = 0.0
    _save_state_locked()
    _set_status(chat_id, force=True)

    try:
        _set_status(chat_id, stage="Pick", detail="Choosing object & anomaly", force=True)

        def _progress(prefix: str, msg: str) -> None:
            _log(prefix, msg)
            p = str(prefix).upper().strip()
            if p == "AO":
                _set_status(chat_id, stage="Pick", detail=msg)
            elif p == "IMAGE":
                _set_status(chat_id, stage="Image", detail=msg)
            elif p == "VIDEO2":
                _set_status(chat_id, stage="Video", detail=msg)

        res: AnomalieObjetResult = generate_one_anomalie_objet(log_fn=_progress)

        _set_status(chat_id, stage="Send", detail="Sending video", force=True)
        clip_id = f"ao_{uuid.uuid4().hex[:10]}"
        clip = Clip(
            clip_id=clip_id,
            video_path=res.video_path,
            object_name=res.object_name,
            anomaly=res.anomaly,
            factual_text=res.factual_text,
            status="pending",
        )
        st.clips[clip_id] = clip
        st.ordered_ids.append(clip_id)
        _save_state_locked()
        _send_clip(chat_id, clip)
        _set_status(chat_id, stage="Send", detail="Clip sent", force=True)

    except Exception as e:
        try:
            _tg_api("sendMessage", data={"chat_id": str(chat_id), "text": f"Generation failed: {e}"})
        except Exception:
            pass
    finally:
        st.generating = False
        st.stage = ""
        st.detail = ""
        _save_state_locked()
        try:
            _main_menu(chat_id, force_new=True)
        except Exception:
            pass


def _handle_callback(chat_id: int, data: str):
    st = _state(chat_id)

    if data == "menu:main":
        _main_menu(chat_id, force_new=True)
        return

    if data == "menu:queue":
        _list_menu(chat_id, "queue")
        return

    if data == "menu:approved":
        _list_menu(chat_id, "approved")
        return

    if data == "menu:rejected":
        _list_menu(chat_id, "rejected")
        return

    if data == "menu:launch":
        if st.generating:
            return
        t = threading.Thread(target=_generate_thread, args=(chat_id,), daemon=True)
        t.start()
        _main_menu(chat_id)
        return

    if data.startswith("clip:open:"):
        cid = data.split(":")[-1]
        clip = st.clips.get(cid)
        if clip:
            _send_clip(chat_id, clip)
        return

    if data.startswith("clip:approve:"):
        cid = data.split(":")[-1]
        clip = st.clips.get(cid)
        if clip:
            clip.status = "approved"
            if cid not in st.approved_ids:
                st.approved_ids.append(cid)
            if cid in st.rejected_ids:
                st.rejected_ids.remove(cid)
            _save_state_locked()
            _main_menu(chat_id)
        return

    if data.startswith("clip:reject:"):
        cid = data.split(":")[-1]
        clip = st.clips.get(cid)
        if clip:
            clip.status = "rejected"
            if cid not in st.rejected_ids:
                st.rejected_ids.append(cid)
            if cid in st.approved_ids:
                st.approved_ids.remove(cid)
            _save_state_locked()
            _main_menu(chat_id)
        return


def _handle_message(chat_id: int, text: str):
    cmd = (text or "").strip().split()[0] if text and text.strip() else ""
    if cmd.startswith("/start"):
        _main_menu(chat_id, force_new=True)
        return


def run() -> None:
    load_anomalie_objet_dotenv()
    token = str(__import__("os").environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing (expected in .env.anomalie_objet or environment)")

    _log("AO_TG", "Starting long-polling (format: anomalie_objet)")
    offset = None

    try:
        while True:
            try:
                r = _tg_api(
                    "getUpdates",
                    params={"timeout": "10", **({"offset": str(offset)} if offset is not None else {})},
                    timeout=25,
                )
                for upd in r or []:
                    offset = int(upd["update_id"]) + 1

                    if "callback_query" in upd:
                        cq = upd["callback_query"]
                        data = cq.get("data") or ""
                        msg = cq.get("message") or {}
                        chat_id = int((msg.get("chat") or {}).get("id"))
                        if chat_id:
                            try:
                                _tg_api("answerCallbackQuery", data={"callback_query_id": cq["id"]})
                            except Exception:
                                pass
                            _handle_callback(chat_id, data)
                        continue

                    msg = upd.get("message")
                    if msg and "text" in msg:
                        chat_id = int((msg.get("chat") or {}).get("id"))
                        if chat_id:
                            _handle_message(chat_id, str(msg.get("text") or ""))

            except Exception as e:
                _log("AO_TG", f"Polling error: {e}")
                time.sleep(2)
    except KeyboardInterrupt:
        _log("AO_TG", "Stopped (KeyboardInterrupt).")


if __name__ == "__main__":
    import os
    import sys
    if str(os.getenv("RUN_LEGACY_BOT", "")).strip() != "1":
        print("Legacy bot disabled. Use: py -m bot.v3.main (set RUN_LEGACY_BOT=1 to run legacy).", flush=True)
        sys.exit(2)
    load_anomalie_objet_dotenv()
    with STATE_LOCK:
        _load_state()
    run()
