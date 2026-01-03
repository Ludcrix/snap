import os
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
import uuid

import requests

from bot.config import TELEGRAM_BOT_TOKEN
from bot.pipeline import ClipResult, generate_one_clip
from bot.telegram.publish_assist import (
    build_publish_caption,
    build_publish_text_from_clip,
    ensure_publish_description,
    get_platform_spec,
    url_button,
)


THEMES = ["injustice", "malaise", "trahison"]

STATE_VERSION = 1
STATE_LOCK = threading.Lock()
STATE_FILE = Path(__file__).resolve().parent.parent / "storage" / "telegram_state.json"
VIDEOS_DIR = Path(__file__).resolve().parent.parent / "storage" / "videos"


def _log(prefix: str, msg: str) -> None:
    print(f"[{prefix}] {msg}")


@dataclass
class Settings:
    num_clips: int = 5
    themes: list[str] = field(default_factory=lambda: THEMES.copy())
    voice_mode: str = "auto"  # auto | male | female


@dataclass
class Clip:
    clip_id: str
    video_path: str
    hook_title: str
    hashtags: list[str]
    publish_desc: str = ""
    status: str = "pending"  # pending | approved | rejected
    message_chat_id: int | None = None
    message_id: int | None = None
    index: int = 0
    total: int = 0


@dataclass
class ChatState:
    settings: Settings = field(default_factory=Settings)
    clips: dict[str, Clip] = field(default_factory=dict)
    ordered_ids: list[str] = field(default_factory=list)
    approved_ids: list[str] = field(default_factory=list)
    rejected_ids: list[str] = field(default_factory=list)
    awaiting_edit_clip_id: str | None = None
    generating: bool = False
    control_message_id: int | None = None
    active_menu: str = "main"  # main | settings | queue | approved | rejected

    # Generation progress (for Telegram status display)
    gen_clip_index: int = 0
    gen_total: int = 0
    gen_stage: str = ""
    gen_detail: str = ""
    gen_last_ui_update_ts: float = 0.0


_CHAT: dict[int, ChatState] = {}


def _ensure_storage_dir() -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _clip_to_dict(c: Clip) -> dict:
    return {
        "clip_id": c.clip_id,
        "video_path": c.video_path,
        "hook_title": c.hook_title,
        "hashtags": c.hashtags,
        "publish_desc": str(getattr(c, "publish_desc", "") or ""),
        "status": c.status,
        "message_chat_id": c.message_chat_id,
        "message_id": c.message_id,
        "index": c.index,
        "total": c.total,
    }


def _state_to_dict(st: ChatState) -> dict:
    return {
        "settings": {
            "num_clips": st.settings.num_clips,
            "themes": list(st.settings.themes),
            "voice_mode": st.settings.voice_mode,
        },
        "clips": {cid: _clip_to_dict(c) for cid, c in st.clips.items()},
        "ordered_ids": list(st.ordered_ids),
        "approved_ids": list(st.approved_ids),
        "rejected_ids": list(st.rejected_ids),
        "awaiting_edit_clip_id": st.awaiting_edit_clip_id,
        # Never persist generating=True across restarts.
        "generating": False,
        "control_message_id": st.control_message_id,
        "active_menu": st.active_menu,
    }


def _dict_to_state(d: dict) -> ChatState:
    st = ChatState()
    settings = d.get("settings") or {}
    st.settings.num_clips = int(settings.get("num_clips") or 5)
    st.settings.themes = [str(x) for x in (settings.get("themes") or []) if str(x).strip()]
    if not st.settings.themes:
        st.settings.themes = THEMES.copy()
    vm = str(settings.get("voice_mode") or "auto")
    st.settings.voice_mode = vm if vm in {"auto", "male", "female"} else "auto"

    clips = d.get("clips") or {}
    if isinstance(clips, dict):
        for cid, cd in clips.items():
            if not isinstance(cd, dict):
                continue
            clip_id = str(cd.get("clip_id") or cid)
            st.clips[clip_id] = Clip(
                clip_id=clip_id,
                video_path=str(cd.get("video_path") or ""),
                hook_title=str(cd.get("hook_title") or ""),
                hashtags=[str(x) for x in (cd.get("hashtags") or []) if str(x).strip()],
                publish_desc=str(cd.get("publish_desc") or ""),
                status=str(cd.get("status") or "pending"),
                message_chat_id=cd.get("message_chat_id"),
                message_id=cd.get("message_id"),
                index=int(cd.get("index") or 0),
                total=int(cd.get("total") or 0),
            )

    st.ordered_ids = [str(x) for x in (d.get("ordered_ids") or []) if str(x).strip()]
    st.approved_ids = [str(x) for x in (d.get("approved_ids") or []) if str(x).strip()]
    st.rejected_ids = [str(x) for x in (d.get("rejected_ids") or []) if str(x).strip()]

    st.awaiting_edit_clip_id = d.get("awaiting_edit_clip_id") if d.get("awaiting_edit_clip_id") else None
    st.control_message_id = d.get("control_message_id")
    am = str(d.get("active_menu") or "main")
    st.active_menu = am if am in {"main", "settings", "queue", "approved", "rejected"} else "main"
    st.generating = False
    return st


def _load_state() -> None:
    if not STATE_FILE.exists():
        return
    try:
        raw = STATE_FILE.read_text(encoding="utf-8")
        import json as _json_mod

        payload = _json_mod.loads(raw)
    except Exception:
        return

    if not isinstance(payload, dict):
        return
    if payload.get("version") != STATE_VERSION:
        return

    chats = payload.get("chats") or {}
    if not isinstance(chats, dict):
        return

    for chat_id_str, st_dict in chats.items():
        try:
            chat_id = int(chat_id_str)
        except Exception:
            continue
        if not isinstance(st_dict, dict):
            continue
        _CHAT[chat_id] = _dict_to_state(st_dict)


def _save_state() -> None:
    _ensure_storage_dir()
    try:
        import json as _json_mod

        payload = {
            "version": STATE_VERSION,
            "chats": {str(cid): _state_to_dict(st) for cid, st in _CHAT.items()},
        }
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(_json_mod.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception:
        # Never crash the bot due to persistence issues.
        return


def _save_state_locked() -> None:
    with STATE_LOCK:
        _save_state()


def _normalize_state(st: ChatState) -> None:
    st.ordered_ids = [cid for cid in st.ordered_ids if cid in st.clips]
    st.approved_ids = [cid for cid in st.approved_ids if cid in st.clips]
    st.rejected_ids = [cid for cid in st.rejected_ids if cid in st.clips]

    # Ensure ids exist in ordered_ids for consistent queue ordering.
    for cid in st.clips.keys():
        if cid not in st.ordered_ids:
            st.ordered_ids.append(cid)

    # Rebuild approved/rejected from clip status (source of truth).
    st.approved_ids = [cid for cid, c in st.clips.items() if c.status == "approved"]
    st.rejected_ids = [cid for cid, c in st.clips.items() if c.status == "rejected"]


def _import_existing_videos_into_chat(chat_id: int) -> int:
    st = _state(chat_id)
    try:
        existing_paths = {Path(c.video_path).resolve() for c in st.clips.values() if c.video_path}
    except Exception:
        existing_paths = set()

    if not VIDEOS_DIR.exists():
        return 0

    imported = 0
    try:
        video_files = [p for p in VIDEOS_DIR.glob("*.mp4") if p.is_file()]
        video_files.sort(key=lambda p: p.stat().st_mtime)
    except Exception:
        return 0

    for p in video_files:
        try:
            rp = p.resolve()
        except Exception:
            rp = p

        if rp in existing_paths:
            continue

        clip_id = f"import_{p.stem}"
        # Ensure unique id even if filename collides.
        if clip_id in st.clips:
            clip_id = f"{clip_id}_{uuid.uuid4().hex[:6]}"

        clip = Clip(
            clip_id=clip_id,
            video_path=str(rp),
            hook_title=p.stem,
            hashtags=[],
            status="pending",
        )
        st.clips[clip_id] = clip
        st.ordered_ids.append(clip_id)
        imported += 1

    _normalize_state(st)
    return imported


def _state(chat_id: int) -> ChatState:
    if chat_id not in _CHAT:
        _CHAT[chat_id] = ChatState()
        _save_state_locked()
    return _CHAT[chat_id]


def _pending_ids(st: ChatState) -> list[str]:
    ids: list[str] = []
    for cid in st.ordered_ids:
        c = st.clips.get(cid)
        if c and c.status == "pending":
            ids.append(cid)
    return ids


def _queue_position(st: ChatState, clip_id: str) -> tuple[int | None, int]:
    pending = _pending_ids(st)
    total = len(pending)
    try:
        pos = pending.index(clip_id) + 1
    except ValueError:
        pos = None
    return pos, total


def _normalize_hashtags(tags: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for t in tags or []:
        s = str(t).strip()
        if not s:
            continue
        if not s.startswith("#"):
            s = "#" + s
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _display_title(clip: Clip) -> str:
    t = str(clip.hook_title or "").strip()
    return t if t else clip.clip_id


def _next_pending_after(st: ChatState, after_clip_id: str | None) -> str | None:
    pending = _pending_ids(st)
    if not pending:
        return None
    if not after_clip_id:
        return pending[0]
    try:
        idx = pending.index(after_clip_id)
    except ValueError:
        return pending[0]
    return pending[idx + 1] if idx + 1 < len(pending) else None


def _send_next_pending(chat_id: int, after_clip_id: str | None) -> None:
    st = _state(chat_id)
    next_id = _next_pending_after(st, after_clip_id)
    if not next_id:
        return
    clip = st.clips.get(next_id)
    if not clip:
        return
    _send_clip(chat_id, clip)


def _advance_after_action(chat_id: int, current_clip_id: str | None) -> None:
    st = _state(chat_id)
    _normalize_state(st)
    _save_state_locked()

    # Prefer continuing moderation flow on pending.
    next_id = _next_pending_after(st, current_clip_id)
    if next_id:
        clip = st.clips.get(next_id)
        if clip:
            _send_clip(chat_id, clip)
        return

    # If no pending, jump user to a meaningful list.
    if st.approved_ids:
        _list_menu(chat_id, "approved")
        _tg_api("sendMessage", data={"chat_id": str(chat_id), "text": "✅ Plus de clips en attente. Ouverture: Approved."})
        return
    if st.rejected_ids:
        _list_menu(chat_id, "rejected")
        _tg_api("sendMessage", data={"chat_id": str(chat_id), "text": "✅ Plus de clips en attente. Ouverture: Rejected."})
        return

    _main_menu(chat_id, force_new=True)
    _tg_api("sendMessage", data={"chat_id": str(chat_id), "text": "✅ Plus de clips en attente."})


def _refresh_menu(chat_id: int) -> None:
    st = _state(chat_id)
    if st.active_menu == "settings":
        _settings_menu(chat_id)
    elif st.active_menu == "queue":
        _list_menu(chat_id, "queue")
    elif st.active_menu == "approved":
        _list_menu(chat_id, "approved")
    elif st.active_menu == "rejected":
        _list_menu(chat_id, "rejected")
    else:
        _main_menu(chat_id)


def _refresh_pending_positions(chat_id: int) -> None:
    st = _state(chat_id)
    for cid in _pending_ids(st):
        c = st.clips.get(cid)
        if c:
            _edit_clip_message(c)


def _tg_api(method: str, *, params=None, data=None, files=None, timeout=60):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing. Put TELEGRAM_BOT_TOKEN=... in .env")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    resp = requests.post(url, params=params, data=data, files=files, timeout=timeout)
    try:
        payload = resp.json()
    except Exception:
        payload = {"ok": False, "description": resp.text}
    if not payload.get("ok"):
        # Telegram returns HTTP 200 with ok=false for many user-level errors.
        # The most common benign one is trying to edit a message with identical content.
        desc = str(payload.get("description") or "")
        code = payload.get("error_code")
        if (
            code == 400
            and "message is not modified" in desc.lower()
            and method in {"editMessageText", "editMessageCaption", "editMessageReplyMarkup"}
        ):
            return None
        raise RuntimeError(f"Telegram API error calling {method}: {payload}")
    return payload.get("result")


def _kb(rows):
    return {"inline_keyboard": rows}


def _btn(text: str, cb: str):
    return {"text": text, "callback_data": cb}


def _json(obj) -> str:
    # Avoid adding extra deps; requests already brings simplejson sometimes, but use stdlib.
    import json as _json_mod

    return _json_mod.dumps(obj, ensure_ascii=False)


def _send_or_edit_panel(chat_id: int, text: str, reply_markup: dict, *, force_new: bool = False):
    st = _state(chat_id)
    if force_new:
        st.control_message_id = None
        _save_state_locked()
    if st.control_message_id is None:
        r = _tg_api(
            "sendMessage",
            data={
                "chat_id": str(chat_id),
                "text": text,
                "reply_markup": _json(reply_markup),
            },
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
    except RuntimeError as e:
        # If the original control message was deleted or can't be edited anymore,
        # fall back to sending a new panel message.
        msg = str(e).lower()
        if any(
            s in msg
            for s in [
                "message to edit not found",
                "message can't be edited",
                "message cannot be edited",
                "chat not found",
            ]
        ):
            st.control_message_id = None
            _save_state_locked()
            _send_or_edit_panel(chat_id, text, reply_markup)
            return
        raise


def _main_menu(chat_id: int, *, force_new: bool = False):
    st = _state(chat_id)
    st.active_menu = "main"
    _save_state_locked()
    text = "Control panel"
    if st.generating:
        text = _format_generation_status(st)

    reply_markup = _kb(
        [
            [_btn("▶️ Launch generation", "menu:launch")],
            [_btn("⚙️ Settings", "menu:settings")],
            [_btn("📋 Queue", "menu:queue")],
            [_btn("✅ Approved clips", "menu:approved")],
            [_btn("❌ Rejected clips", "menu:rejected")],
        ]
    )
    _send_or_edit_panel(chat_id, text, reply_markup, force_new=force_new)


def _format_generation_status(st: ChatState) -> str:
    # Clean, sequential display. Keep it short to avoid Telegram limits.
    header = "Control panel\n\nStatus: generating"
    if st.gen_total and st.gen_clip_index:
        header += f"\nClip: {st.gen_clip_index}/{st.gen_total}"
    lines: list[str] = [header]

    if st.gen_stage:
        lines.append(f"Step: {st.gen_stage}")
    if st.gen_detail:
        detail = st.gen_detail.strip().replace("\r", " ").replace("\n", " ")
        if len(detail) > 140:
            detail = detail[:137] + "…"
        lines.append(f"Info: {detail}")

    lines.append("\nUpdates: Story → Voice → Image → Video → Send")
    return "\n".join(lines)


def _set_generation_status(
    chat_id: int,
    *,
    clip_index: int | None = None,
    total: int | None = None,
    stage: str | None = None,
    detail: str | None = None,
    force: bool = False,
) -> None:
    st = _state(chat_id)
    if clip_index is not None:
        st.gen_clip_index = int(clip_index)
    if total is not None:
        st.gen_total = int(total)
    if stage is not None:
        st.gen_stage = str(stage)
    if detail is not None:
        st.gen_detail = str(detail)

    now = time.time()
    # Throttle UI updates to avoid spamming editMessageText.
    if not force and (now - float(st.gen_last_ui_update_ts or 0.0)) < 1.0:
        return
    st.gen_last_ui_update_ts = now

    try:
        _main_menu(chat_id)
    except Exception:
        # Never crash generation due to UI update issues.
        pass


def _settings_menu(chat_id: int):
    st = _state(chat_id)
    st.active_menu = "settings"
    _save_state_locked()

    themes_txt = ", ".join(st.settings.themes) if st.settings.themes else "(none)"
    text = (
        "Settings\n"
        f"- Clips: {st.settings.num_clips}\n"
        f"- Themes: {themes_txt}\n"
        f"- Voice mode: {st.settings.voice_mode}"
    )

    num_row = [
        _btn("5" + (" ✅" if st.settings.num_clips == 5 else ""), "set:num:5"),
        _btn("10" + (" ✅" if st.settings.num_clips == 10 else ""), "set:num:10"),
        _btn("20" + (" ✅" if st.settings.num_clips == 20 else ""), "set:num:20"),
    ]

    voice_row = [
        _btn("auto" + (" ✅" if st.settings.voice_mode == "auto" else ""), "set:voice:auto"),
        _btn("male" + (" ✅" if st.settings.voice_mode == "male" else ""), "set:voice:male"),
        _btn("female" + (" ✅" if st.settings.voice_mode == "female" else ""), "set:voice:female"),
    ]

    theme_rows = []
    for t in THEMES:
        on = t in st.settings.themes
        theme_rows.append([_btn(("✅ " if on else "☑️ ") + t, f"set:theme:{t}")])

    reply_markup = _kb(
        [
            num_row,
            voice_row,
            *_theme_rows_or_empty(theme_rows),
            [_btn("⬅️ Back", "menu:main")],
        ]
    )
    _send_or_edit_panel(chat_id, text, reply_markup)


def _theme_rows_or_empty(theme_rows: list[list[dict]]):
    return theme_rows if theme_rows else [[_btn("(no themes)", "noop")]]


def _list_menu(chat_id: int, which: str):
    st = _state(chat_id)
    st.active_menu = which
    _normalize_state(st)
    _save_state_locked()

    if which == "queue":
        ids = _pending_ids(st)
        title = "Queue"
    elif which == "approved":
        ids = st.approved_ids
        title = "Approved clips"
    else:
        ids = st.rejected_ids
        title = "Rejected clips"

    max_items = 10
    shown_ids = ids[:max_items]

    # Text summary with title + hashtags
    lines: list[str] = [f"{title}", f"Count: {len(ids)}"]
    if len(ids) > max_items:
        lines.append(f"Showing first {max_items} (use actions to move/approve/reject).")
    lines.append("")

    for idx, cid in enumerate(shown_ids, start=1):
        c = st.clips.get(cid)
        if not c:
            continue
        tags = " ".join(_normalize_hashtags(c.hashtags))
        pos, total = _queue_position(st, cid)
        qp = "-" if pos is None else f"{pos}/{total}" if total else str(pos)
        title_txt = _display_title(c).strip()
        if len(title_txt) > 60:
            title_txt = title_txt[:57] + "…"
        if tags:
            lines.append(f"{idx}. {title_txt}  ({c.status})  [#{qp}]")
            lines.append(f"   {tags}")
        else:
            lines.append(f"{idx}. {title_txt}  ({c.status})  [#{qp}]")
            lines.append("   (no hashtags)")

    text = "\n".join(lines)

    # Quick action rows per clip
    rows: list[list[dict]] = []
    for cid in shown_ids:
        c = st.clips.get(cid)
        if not c:
            continue
        rows.append(
            [
                _btn("🎬 Preview", f"clip:open:{cid}"),
                _btn("✅", f"clip:approve:{cid}"),
                _btn("❌", f"clip:reject:{cid}"),
                _btn("⬇️", f"clip:later:{cid}"),
                _btn("🗑️", f"clip:delete:{cid}"),
            ]
        )

    rows.append([_btn("⬅️ Back", "menu:main")])
    _send_or_edit_panel(chat_id, text, _kb(rows))


def _clip_caption(chat_id: int, clip: Clip) -> str:
    st = _state(chat_id)
    pos, total = _queue_position(st, clip.clip_id)
    qp = "-" if pos is None else (f"{pos}/{total}" if total else str(pos))
    return build_publish_caption(
        clip,
        platform="snap",
        status_line=f"Status: {clip.status}",
        queue_line=f"Queue position: {qp}",
    )


def _clip_actions_kb(clip_id: str):
    spec = get_platform_spec("snap")
    return _kb(
        [
            [
                url_button("📤 Publier sur Snap", spec.deeplink),
                _btn("📋 Copier texte", f"pub:copy:snap:{clip_id}"),
            ],
            [
                _btn("✅ Approve", f"clip:approve:{clip_id}"),
                _btn("❌ Reject", f"clip:reject:{clip_id}"),
            ],
            [
                _btn("✏️ Edit text", f"clip:edit:{clip_id}"),
                _btn("⏸️ Later", f"clip:later:{clip_id}"),
            ],
            [_btn("🗑️ Delete", f"clip:delete:{clip_id}")],
            [_btn("⬅️ Back", "menu:main")],
        ]
    )


def _list_kind_for_clip(clip: Clip) -> str:
    # queue | approved | rejected
    if clip.status == "approved":
        return "approved"
    if clip.status == "rejected":
        return "rejected"
    return "queue"


def _ids_for_list(st: ChatState, which: str) -> list[str]:
    if which == "approved":
        return list(st.approved_ids)
    if which == "rejected":
        return list(st.rejected_ids)
    return _pending_ids(st)


def _pick_next_after(st: ChatState, which: str, current_id: str) -> str | None:
    ids = _ids_for_list(st, which)
    try:
        idx = ids.index(current_id)
    except ValueError:
        return ids[0] if ids else None
    return ids[idx + 1] if idx + 1 < len(ids) else None


def _safe_delete_video_file(video_path: str) -> bool:
    # Only delete files inside storage/videos to avoid deleting arbitrary user files.
    try:
        p = Path(video_path)
        rp = p.resolve()
        vd = VIDEOS_DIR.resolve()
        if rp == vd or vd not in rp.parents:
            return False
        if rp.is_file():
            rp.unlink()
            return True
    except Exception:
        return False
    return False


def _delete_clip_and_open_next(chat_id: int, clip_id: str) -> None:
    st = _state(chat_id)
    clip = st.clips.get(clip_id)
    if not clip:
        return

    # Decide what "next" means before we mutate state.
    current_list = _list_kind_for_clip(clip)
    next_same_list = _pick_next_after(st, current_list, clip_id)

    # Best-effort: delete the video message to keep chat clean.
    try:
        if clip.message_chat_id is not None and clip.message_id is not None:
            _tg_api(
                "deleteMessage",
                data={
                    "chat_id": str(int(clip.message_chat_id)),
                    "message_id": str(int(clip.message_id)),
                },
            )
    except Exception:
        pass

    # Remove from state.
    if st.awaiting_edit_clip_id == clip_id:
        st.awaiting_edit_clip_id = None
    st.clips.pop(clip_id, None)
    st.ordered_ids = [x for x in st.ordered_ids if x != clip_id]
    st.approved_ids = [x for x in st.approved_ids if x != clip_id]
    st.rejected_ids = [x for x in st.rejected_ids if x != clip_id]
    _normalize_state(st)
    _save_state_locked()

    # Delete file from disk (safe scope).
    deleted_file = _safe_delete_video_file(clip.video_path)

    # Refresh panels and queue positions.
    _refresh_pending_positions(chat_id)
    _refresh_menu(chat_id)

    try:
        msg = "🗑️ Clip supprimé."
        if deleted_file:
            msg += " (vidéo supprimée)"
        _tg_api("sendMessage", data={"chat_id": str(chat_id), "text": msg})
    except Exception:
        pass

    # Open next in same list, else first in same list, else other lists, else main.
    if next_same_list and next_same_list in st.clips:
        _send_clip(chat_id, st.clips[next_same_list])
        return

    remaining_same = _ids_for_list(st, current_list)
    if remaining_same:
        first_id = remaining_same[0]
        c = st.clips.get(first_id)
        if c:
            _send_clip(chat_id, c)
            return

    # Fallback to other lists
    for which in ["queue", "approved", "rejected"]:
        ids = _ids_for_list(st, which)
        if ids:
            c = st.clips.get(ids[0])
            if c:
                _send_clip(chat_id, c)
                return

    _main_menu(chat_id, force_new=True)


def _edit_cancel_kb():
    return _kb([[_btn("⬅️ Back", "edit:cancel")]])


def _send_clip(chat_id: int, clip: Clip):
    caption = _clip_caption(chat_id, clip)
    with open(clip.video_path, "rb") as f:
        r = _tg_api(
            "sendVideo",
            data={
                "chat_id": str(chat_id),
                "caption": caption,
                "reply_markup": _json(_clip_actions_kb(clip.clip_id)),
            },
            files={"video": f},
            timeout=300,
        )
    clip.message_chat_id = int(r["chat"]["id"])
    clip.message_id = int(r["message_id"])
    _save_state_locked()


def _edit_clip_message(clip: Clip):
    if clip.message_chat_id is None or clip.message_id is None:
        return
    chat_id = int(clip.message_chat_id)
    _tg_api(
        "editMessageCaption",
        data={
            "chat_id": str(chat_id),
            "message_id": str(clip.message_id),
            "caption": _clip_caption(chat_id, clip),
            "reply_markup": _json(_clip_actions_kb(clip.clip_id)),
        },
    )
    _save_state_locked()


def _generate_thread(chat_id: int):
    st = _state(chat_id)
    st.generating = True
    try:
        st.gen_clip_index = 0
        st.gen_total = int(st.settings.num_clips)
        st.gen_stage = "Starting"
        st.gen_detail = "Preparing generation"
        st.gen_last_ui_update_ts = 0.0
        _set_generation_status(chat_id, force=True)

        total = st.settings.num_clips
        for i in range(total):
            try:
                _log("TELEGRAM", f"Generating clip {i+1}/{total} for chat_id={chat_id}")

                _set_generation_status(
                    chat_id,
                    clip_index=i + 1,
                    total=total,
                    stage="Story",
                    detail="Requesting story & visual metadata",
                    force=True,
                )

                def _tg_progress(prefix: str, msg: str) -> None:
                    # Keep terminal logging as-is.
                    _log(prefix, msg)

                    p = str(prefix).upper().strip()
                    # Map internal logs to a clean sequential stage.
                    if p == "STORY":
                        _set_generation_status(chat_id, stage="Story", detail=msg)
                    elif p == "VOICE":
                        _set_generation_status(chat_id, stage="Voice", detail=msg)
                    elif p == "IMAGE":
                        _set_generation_status(chat_id, stage="Image", detail=msg)
                    elif p == "VIDEO":
                        _set_generation_status(chat_id, stage="Video", detail=msg)

                clip_res: ClipResult = generate_one_clip(
                    themes=st.settings.themes,
                    voice_mode=st.settings.voice_mode,
                    log_fn=_tg_progress,
                )

                _set_generation_status(chat_id, stage="Send", detail="Sending video to Telegram", force=True)

                clip_id = f"clip_{uuid.uuid4().hex[:10]}"
                clip = Clip(
                    clip_id=clip_id,
                    video_path=clip_res.video_path,
                    hook_title=clip_res.hook_title,
                    hashtags=clip_res.hashtags,
                    status="pending",
                    index=0,
                    total=0,
                )
                st.clips[clip_id] = clip
                st.ordered_ids.append(clip_id)
                _normalize_state(st)
                _save_state_locked()
                _send_clip(chat_id, clip)
                _set_generation_status(chat_id, stage="Send", detail="Clip sent. Waiting for your decision…", force=True)
                _refresh_pending_positions(chat_id)
            except Exception as e:
                _tg_api(
                    "sendMessage",
                    data={"chat_id": str(chat_id), "text": f"Generation failed: {e}"},
                )
                break
    finally:
        st.generating = False
        st.gen_stage = ""
        st.gen_detail = ""
        st.gen_clip_index = 0
        st.gen_total = 0
        try:
            _main_menu(chat_id, force_new=True)
        except Exception:
            pass


def _handle_callback(chat_id: int, data: str):
    st = _state(chat_id)

    if data == "menu:main":
        _main_menu(chat_id, force_new=True)
        return

    if data.startswith("pub:copy:"):
        # pub:copy:<platform>:<clip_id>
        parts = data.split(":")
        if len(parts) >= 4:
            platform = parts[2]
            cid = parts[3]
        else:
            platform = "snap"
            cid = parts[-1] if parts else ""
        clip = st.clips.get(cid)
        if clip:
            # Ensure description exists (V1) and persist it.
            ensure_publish_description(clip)
            _save_state_locked()
            text = build_publish_text_from_clip(clip, platform=platform)
            _tg_api("sendMessage", data={"chat_id": str(chat_id), "text": text})
        return
    if data == "menu:settings":
        _settings_menu(chat_id)
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

    if data == "edit:cancel":
        st.awaiting_edit_clip_id = None
        _save_state_locked()
        _main_menu(chat_id, force_new=True)
        return

    if data == "menu:launch":
        if st.generating:
            return
        t = threading.Thread(target=_generate_thread, args=(chat_id,), daemon=True)
        t.start()
        _main_menu(chat_id)
        return

    if data.startswith("set:num:"):
        v = int(data.split(":")[-1])
        if v in {5, 10, 20}:
            st.settings.num_clips = v
            _save_state_locked()
        _settings_menu(chat_id)
        return

    if data.startswith("set:voice:"):
        v = data.split(":")[-1]
        if v in {"auto", "male", "female"}:
            st.settings.voice_mode = v
            _save_state_locked()
        _settings_menu(chat_id)
        return

    if data.startswith("set:theme:"):
        t = data.split(":")[-1]
        if t in THEMES:
            if t in st.settings.themes:
                st.settings.themes = [x for x in st.settings.themes if x != t]
            else:
                st.settings.themes.append(t)
            _save_state_locked()
        _settings_menu(chat_id)
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
            _normalize_state(st)
            _save_state_locked()
            _edit_clip_message(clip)
            _refresh_pending_positions(chat_id)
            _refresh_menu(chat_id)
            _advance_after_action(chat_id, cid)
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
            _normalize_state(st)
            _save_state_locked()
            _edit_clip_message(clip)
            _refresh_pending_positions(chat_id)
            _refresh_menu(chat_id)
            _advance_after_action(chat_id, cid)
        return

    if data.startswith("clip:later:"):
        # Move back to pending and place at end of queue.
        cid = data.split(":")[-1]
        clip = st.clips.get(cid)
        if clip:
            clip.status = "pending"
        if cid in st.approved_ids:
            st.approved_ids = [x for x in st.approved_ids if x != cid]
        if cid in st.rejected_ids:
            st.rejected_ids = [x for x in st.rejected_ids if x != cid]
        if cid in st.ordered_ids:
            st.ordered_ids = [x for x in st.ordered_ids if x != cid]
            st.ordered_ids.append(cid)
            _normalize_state(st)
            _save_state_locked()
        if clip:
            _edit_clip_message(clip)
        _refresh_pending_positions(chat_id)
        _refresh_menu(chat_id)
        _advance_after_action(chat_id, cid)
        return

    if data.startswith("clip:edit:"):
        cid = data.split(":")[-1]
        if cid in st.clips:
            st.awaiting_edit_clip_id = cid
            _save_state_locked()
            _tg_api(
                "sendMessage",
                data={
                    "chat_id": str(chat_id),
                    "text": "Send the new title, then optionally a second line with hashtags (e.g. #snap #story).",
                    "reply_markup": _json(_edit_cancel_kb()),
                },
            )
        return

    if data.startswith("clip:delete:"):
        cid = data.split(":")[-1]
        _delete_clip_and_open_next(chat_id, cid)
        return


def _handle_message(chat_id: int, text: str):
    st = _state(chat_id)
    cmd = text.strip().split()[0] if text and text.strip() else ""
    if cmd.startswith("/start"):
        # Ensure previous generations (videos on disk) are visible after restart
        # and always send a fresh panel so the menu is visible at the bottom.
        _import_existing_videos_into_chat(chat_id)
        _normalize_state(st)
        _save_state_locked()
        _main_menu(chat_id, force_new=True)
        return

    if cmd == "/queue":
        _list_menu(chat_id, "queue")
        return
    if cmd == "/approved":
        _list_menu(chat_id, "approved")
        return
    if cmd == "/rejected":
        _list_menu(chat_id, "rejected")
        return
    if cmd == "/settings":
        _settings_menu(chat_id)
        return

    if st.awaiting_edit_clip_id:
        cid = st.awaiting_edit_clip_id
        clip = st.clips.get(cid)
        if not clip:
            st.awaiting_edit_clip_id = None
            return

        lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
        if not lines:
            return

        new_title = lines[0]
        if new_title:
            clip.hook_title = new_title

        if len(lines) >= 2:
            raw_tags = lines[1].split()
            tags = [t.strip() for t in raw_tags if t.strip()]
            if tags:
                clip.hashtags = tags

        st.awaiting_edit_clip_id = None
        _save_state_locked()
        _edit_clip_message(clip)
        _refresh_menu(chat_id)
        _tg_api("sendMessage", data={"chat_id": str(chat_id), "text": "Updated."})


def _startup_ready_and_restore() -> None:
    # Import existing videos so previous generations appear in Queue after restart.
    for chat_id in list(_CHAT.keys()):
        try:
            imported = _import_existing_videos_into_chat(chat_id)
            _normalize_state(_CHAT[chat_id])
            _save_state_locked()
            _tg_api(
                "sendMessage",
                data={
                    "chat_id": str(chat_id),
                    "text": f"✅ Bot prêt. État restauré. Clips importés: {imported}.\nEnvoie /start pour afficher le menu.",
                },
            )
            # Send a fresh panel so the menu is visible at the bottom.
            _main_menu(chat_id, force_new=True)
        except Exception:
            # Don't block startup if a chat can't be notified.
            continue


def run():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing. Put TELEGRAM_BOT_TOKEN=... in .env")

    _log("TELEGRAM", "Starting long-polling")
    offset = None

    try:
        while True:
            try:
                r = _tg_api(
                    "getUpdates",
                    params={
                        # Keep polling latency low so the bot stays responsive during long generations.
                        "timeout": "10",
                        **({"offset": str(offset)} if offset is not None else {}),
                    },
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
                _log("TELEGRAM", f"Polling error: {e}")
                time.sleep(2)
    except KeyboardInterrupt:
        _log("TELEGRAM", "Stopped (KeyboardInterrupt). Restart with: py -m bot.telegram_control")


if __name__ == "__main__":
    import os
    import sys
    if str(os.getenv("RUN_LEGACY_BOT", "")).strip() != "1":
        print("Legacy bot disabled. Use: py -m bot.v3.main (set RUN_LEGACY_BOT=1 to run legacy).", flush=True)
        sys.exit(2)
    with STATE_LOCK:
        _load_state()
    _startup_ready_and_restore()
    run()
