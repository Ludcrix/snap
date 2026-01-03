from __future__ import annotations

"""Integration layer that extends the existing V1 Telegram bot without modifying its files.

This module monkey-patches the V1 module at runtime to add a new menu entry and callbacks
for the V2 format: "Anomalie visuelle – Objet".

Constraint-friendly: V1 source files remain unchanged.
"""

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from bot.formats.anomalie_objet.env import load_anomalie_objet_dotenv
from bot.formats.anomalie_objet.pipeline import generate_one_anomalie_objet
from bot.telegram.publish_assist import (
    build_publish_caption,
    build_publish_text_from_clip,
    get_platform_spec,
    url_button,
)


STATE_LOCK = threading.Lock()
STATE_FILE = Path(__file__).resolve().parents[3] / "storage" / "formats" / "anomalie_objet" / "integration_state.json"
AO_VIDEOS_DIR = (Path(__file__).resolve().parents[3] / "storage" / "formats" / "anomalie_objet" / "videos").resolve()


@dataclass
class AOChatSettings:
    num_clips: int = 3


@dataclass
class AOClip:
    clip_id: str
    video_path: str
    hook_title: str
    hashtags: list[str]
    snap_hook: str = ""
    status: str = "pending"  # pending | approved | rejected
    message_chat_id: int | None = None
    message_id: int | None = None
    index: int = 0
    total: int = 0


@dataclass
class AOChatState:
    settings: AOChatSettings = field(default_factory=AOChatSettings)
    clips: dict[str, AOClip] = field(default_factory=dict)
    ordered_ids: list[str] = field(default_factory=list)
    approved_ids: list[str] = field(default_factory=list)
    rejected_ids: list[str] = field(default_factory=list)
    generating: bool = False
    stage: str = ""
    detail: str = ""
    last_ui_ts: float = 0.0


_AO: dict[int, AOChatState] = {}


def _ensure_storage_dir() -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _known_video_paths_lower() -> set[str]:
    out: set[str] = set()
    for st in _AO.values():
        for c in st.clips.values():
            try:
                p = Path(str(c.video_path or "")).resolve()
            except Exception:
                continue
            out.add(str(p).lower())
    return out


def _auto_import_local_videos(chat_id: int) -> int:
    """Import existing AO mp4 files into this chat state.

    This is a recovery mechanism: if the integration state was lost/emptied but
    generated assets are still present on disk, we rebuild a minimal queue so
    clips remain accessible after restart.
    """

    st = _st(chat_id)
    try:
        AO_VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    known = _known_video_paths_lower()
    imported = 0

    try:
        candidates = sorted(
            [p for p in AO_VIDEOS_DIR.glob("*.mp4") if p.is_file()],
            key=lambda p: (p.stat().st_mtime, p.name.lower()),
        )
    except Exception:
        candidates = []

    for p in candidates:
        try:
            rp = p.resolve()
        except Exception:
            continue

        if str(rp).lower() in known:
            continue

        clip_id = p.stem.strip() or f"ao_{uuid.uuid4().hex[:10]}"
        if clip_id in st.clips:
            continue

        st.clips[clip_id] = AOClip(
            clip_id=clip_id,
            video_path=str(rp),
            hook_title=clip_id,
            hashtags=[],
            snap_hook="",
            status="pending",
            index=0,
            total=0,
        )
        st.ordered_ids.append(clip_id)
        imported += 1
        known.add(str(rp).lower())

    if imported:
        _normalize_state(st)
        _save_state_locked()
    return imported


def _save_state() -> None:
    _ensure_storage_dir()
    try:
        import json

        payload = {
            "version": 2,
            "chats": {
                str(cid): {
                    "settings": {"num_clips": int(st.settings.num_clips)},
                    "clips": {
                        clip_id: {
                            "clip_id": c.clip_id,
                            "video_path": c.video_path,
                            "hook_title": c.hook_title,
                            "hashtags": list(c.hashtags),
                            "snap_hook": str(getattr(c, "snap_hook", "") or ""),
                            "status": c.status,
                            "message_chat_id": c.message_chat_id,
                            "message_id": c.message_id,
                            "index": c.index,
                            "total": c.total,
                        }
                        for clip_id, c in st.clips.items()
                    },
                    "ordered_ids": list(st.ordered_ids),
                    "approved_ids": list(st.approved_ids),
                    "rejected_ids": list(st.rejected_ids),
                }
                for cid, st in _AO.items()
            },
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
            st = AOChatState()
            settings = st_d.get("settings") or {}
            try:
                st.settings.num_clips = int(settings.get("num_clips") or st.settings.num_clips)
            except Exception:
                pass

            clips = st_d.get("clips") or {}
            if isinstance(clips, dict):
                for clip_id, cd in clips.items():
                    if not isinstance(cd, dict):
                        continue
                    cid2 = str(cd.get("clip_id") or clip_id).strip()
                    if not cid2:
                        continue
                    st.clips[cid2] = AOClip(
                        clip_id=cid2,
                        video_path=str(cd.get("video_path") or ""),
                        hook_title=str(cd.get("hook_title") or "").strip(),
                        hashtags=[str(x) for x in (cd.get("hashtags") or []) if str(x).strip()],
                        snap_hook=str(cd.get("snap_hook") or "").strip(),
                        status=str(cd.get("status") or "pending"),
                        message_chat_id=cd.get("message_chat_id"),
                        message_id=cd.get("message_id"),
                        index=int(cd.get("index") or 0),
                        total=int(cd.get("total") or 0),
                    )

            st.ordered_ids = [str(x) for x in (st_d.get("ordered_ids") or []) if str(x).strip()]
            st.approved_ids = [str(x) for x in (st_d.get("approved_ids") or []) if str(x).strip()]
            st.rejected_ids = [str(x) for x in (st_d.get("rejected_ids") or []) if str(x).strip()]
            _normalize_state(st)
            _AO[cid] = st
    except Exception:
        return


def _st(chat_id: int) -> AOChatState:
    if chat_id not in _AO:
        _AO[chat_id] = AOChatState()
    return _AO[chat_id]


def _normalize_state(st: AOChatState) -> None:
    # Drop clips whose underlying file is missing.
    missing: list[str] = []
    for cid, c in list(st.clips.items()):
        try:
            vp = Path(str(c.video_path or "")).resolve()
        except Exception:
            missing.append(cid)
            continue
        if not vp.is_file():
            missing.append(cid)
    for cid in missing:
        st.clips.pop(cid, None)

    st.ordered_ids = [cid for cid in st.ordered_ids if cid in st.clips]
    st.approved_ids = [cid for cid in st.approved_ids if cid in st.clips]
    st.rejected_ids = [cid for cid in st.rejected_ids if cid in st.clips]

    for cid in st.clips.keys():
        if cid not in st.ordered_ids:
            st.ordered_ids.append(cid)

    st.approved_ids = [cid for cid, c in st.clips.items() if c.status == "approved"]
    st.rejected_ids = [cid for cid, c in st.clips.items() if c.status == "rejected"]


def _pending_ids(st: AOChatState) -> list[str]:
    ids: list[str] = []
    for cid in st.ordered_ids:
        c = st.clips.get(cid)
        if c and c.status == "pending":
            ids.append(cid)
    return ids


def _queue_position(st: AOChatState, clip_id: str) -> tuple[int | None, int]:
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


def _clip_caption(chat_id: int, clip: AOClip) -> str:
    st = _st(chat_id)
    pos, total = _queue_position(st, clip.clip_id)
    qp = "-" if pos is None else (f"{pos}/{total}" if total else str(pos))
    return build_publish_caption(
        clip,
        platform="snap",
        status_line=f"Status: {clip.status}",
        queue_line=f"Queue position: {qp}",
    )


def _format_panel(v1, chat_id: int) -> str:
    st = _st(chat_id)
    header = "V2 — Anomalie Objet"
    if st.generating:
        lines = [header, "", "Statut: génération en cours"]
        if st.stage:
            lines.append(f"Step: {st.stage}")
        if st.detail:
            d = st.detail.replace("\n", " ").strip()
            if len(d) > 160:
                d = d[:157] + "…"
            lines.append(f"Info: {d}")
        lines.append("\nÉtapes: Choix → Image → Vidéo")
        return "\n".join(lines)

    q = len(_pending_ids(st))
    a = len(st.approved_ids)
    r = len(st.rejected_ids)
    return f"{header}\n\nQueue: {q} | Approved: {a} | Rejected: {r}"


def _ao_menu(v1, chat_id: int, *, force_new: bool = False) -> None:
    st = _st(chat_id)
    # Recovery: if clips exist on disk but state is empty, rebuild queue.
    if not st.clips:
        _auto_import_local_videos(chat_id)
    current = int(st.settings.num_clips or 1)
    reply = v1._kb(
        [
            [v1._btn(f"Nombre de clips: {current}", "noop")],
            [
                v1._btn("1", "v2:ao:setcount:1"),
                v1._btn("3", "v2:ao:setcount:3"),
                v1._btn("5", "v2:ao:setcount:5"),
                v1._btn("10", "v2:ao:setcount:10"),
            ],
            [v1._btn("▶️ Lancer génération", "v2:ao:launch")],
            [v1._btn("📋 Queue", "v2:ao:queue")],
            [v1._btn("✅ Approved", "v2:ao:approved")],
            [v1._btn("❌ Rejected", "v2:ao:rejected")],
            [v1._btn("⬅️ Back", "menu:main")],
        ]
    )
    v1._send_or_edit_panel(chat_id, _format_panel(v1, chat_id), reply, force_new=force_new)


def _set_status(v1, chat_id: int, *, stage: str | None = None, detail: str | None = None, force: bool = False) -> None:
    st = _st(chat_id)
    if stage is not None:
        st.stage = stage
    if detail is not None:
        st.detail = detail

    now = time.time()
    if not force and (now - float(st.last_ui_ts or 0.0)) < 1.0:
        return
    st.last_ui_ts = now

    try:
        _ao_menu(v1, chat_id)
    except Exception:
        pass


def _clip_actions_kb(v1, clip_id: str):
    spec = get_platform_spec("snap")
    return v1._kb(
        [
            [
                url_button("📤 Publier sur Snap", spec.deeplink),
                v1._btn("📋 Copier texte", f"v2:ao:clip:copytext:{clip_id}"),
            ],
            [
                v1._btn("✅ Approve", f"v2:ao:clip:approve:{clip_id}"),
                v1._btn("❌ Reject", f"v2:ao:clip:reject:{clip_id}"),
            ],
            [
                v1._btn("⏸️ Later", f"v2:ao:clip:later:{clip_id}"),
                v1._btn("🗑️ Delete", f"v2:ao:clip:delete:{clip_id}"),
            ],
            [v1._btn("⬅️ Back", "v2:ao:menu")],
        ]
    )


def _send_clip(v1, chat_id: int, clip: AOClip) -> None:
    with open(clip.video_path, "rb") as f:
        r = v1._tg_api(
            "sendVideo",
            data={
                "chat_id": str(chat_id),
                "caption": _clip_caption(chat_id, clip),
                "reply_markup": v1._json(_clip_actions_kb(v1, clip.clip_id)),
            },
            files={"video": f},
            timeout=300,
        )
    clip.message_chat_id = int(r["chat"]["id"])
    clip.message_id = int(r["message_id"])
    _save_state_locked()


def _edit_clip_message(v1, chat_id: int, clip: AOClip) -> None:
    if clip.message_chat_id is None or clip.message_id is None:
        return
    v1._tg_api(
        "editMessageCaption",
        data={
            "chat_id": str(int(clip.message_chat_id)),
            "message_id": str(int(clip.message_id)),
            "caption": _clip_caption(chat_id, clip),
            "reply_markup": v1._json(_clip_actions_kb(v1, clip.clip_id)),
        },
    )
    _save_state_locked()


def _list_menu(v1, chat_id: int, which: str) -> None:
    st = _st(chat_id)
    if not st.clips:
        _auto_import_local_videos(chat_id)
    _normalize_state(st)
    _save_state_locked()

    if which == "queue":
        ids = _pending_ids(st)
        title = "Queue"
    elif which == "approved":
        ids = [cid for cid in st.approved_ids if cid in st.clips]
        title = "Approved clips"
    else:
        ids = [cid for cid in st.rejected_ids if cid in st.clips]
        title = "Rejected clips"

    max_items = 10
    shown_ids = ids[:max_items]

    lines: list[str] = ["V2 — Anomalie Objet", f"{title}", f"Count: {len(ids)}"]
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
        title_txt = (str(c.hook_title or "").strip() or cid)
        if len(title_txt) > 60:
            title_txt = title_txt[:57] + "…"
        lines.append(f"{idx}. {title_txt}  ({c.status})  [#{qp}]")
        lines.append(f"   {tags}" if tags else "   (no hashtags)")

    rows: list[list[dict]] = []
    for cid in shown_ids:
        rows.append(
            [
                v1._btn("🎬 Preview", f"v2:ao:clip:open:{cid}"),
                v1._btn("✅", f"v2:ao:clip:approve:{cid}"),
                v1._btn("❌", f"v2:ao:clip:reject:{cid}"),
                v1._btn("⬇️", f"v2:ao:clip:later:{cid}"),
                v1._btn("🗑️", f"v2:ao:clip:delete:{cid}"),
            ]
        )

    rows.append([v1._btn("⬅️ Back", "v2:ao:menu")])
    v1._send_or_edit_panel(chat_id, "\n".join(lines), v1._kb(rows))


def _next_pending_after(st: AOChatState, after_clip_id: str | None) -> str | None:
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


def _advance_after_action(v1, chat_id: int, current_clip_id: str | None) -> None:
    st = _st(chat_id)
    _normalize_state(st)
    _save_state_locked()

    next_id = _next_pending_after(st, current_clip_id)
    if next_id:
        clip = st.clips.get(next_id)
        if clip:
            _send_clip(v1, chat_id, clip)
        return

    if st.approved_ids:
        _list_menu(v1, chat_id, "approved")
        v1._tg_api("sendMessage", data={"chat_id": str(chat_id), "text": "✅ Plus de clips en attente. Ouverture: Approved."})
        return
    if st.rejected_ids:
        _list_menu(v1, chat_id, "rejected")
        v1._tg_api("sendMessage", data={"chat_id": str(chat_id), "text": "✅ Plus de clips en attente. Ouverture: Rejected."})
        return

    _ao_menu(v1, chat_id, force_new=True)
    v1._tg_api("sendMessage", data={"chat_id": str(chat_id), "text": "✅ Plus de clips en attente."})


def _safe_delete_video_file(video_path: str) -> bool:
    try:
        rp = Path(video_path).resolve()
        if rp == AO_VIDEOS_DIR or AO_VIDEOS_DIR not in rp.parents:
            return False
        if rp.is_file():
            rp.unlink()
            return True
    except Exception:
        return False
    return False


def _delete_clip_and_open_next(v1, chat_id: int, clip_id: str) -> None:
    st = _st(chat_id)
    clip = st.clips.get(clip_id)
    if not clip:
        return

    next_id = _next_pending_after(st, clip_id)

    try:
        if clip.message_chat_id is not None and clip.message_id is not None:
            v1._tg_api(
                "deleteMessage",
                data={
                    "chat_id": str(int(clip.message_chat_id)),
                    "message_id": str(int(clip.message_id)),
                },
            )
    except Exception:
        pass

    st.clips.pop(clip_id, None)
    st.ordered_ids = [x for x in st.ordered_ids if x != clip_id]
    st.approved_ids = [x for x in st.approved_ids if x != clip_id]
    st.rejected_ids = [x for x in st.rejected_ids if x != clip_id]
    _normalize_state(st)
    _save_state_locked()

    _safe_delete_video_file(clip.video_path)

    if next_id:
        nxt = st.clips.get(next_id)
        if nxt:
            _send_clip(v1, chat_id, nxt)
            return

    for which in ["queue", "approved", "rejected"]:
        ids = _pending_ids(st) if which == "queue" else (st.approved_ids if which == "approved" else st.rejected_ids)
        ids = [cid for cid in ids if cid in st.clips]
        if ids:
            _send_clip(v1, chat_id, st.clips[ids[0]])
            return

    _ao_menu(v1, chat_id, force_new=True)

def _generate_thread(v1, chat_id: int) -> None:
    # Load duplicated env so V2 can run with its own keys/tokens.
    load_anomalie_objet_dotenv()

    st = _st(chat_id)
    st.generating = True
    st.stage = "Starting"
    st.detail = "Preparing"
    st.last_ui_ts = 0.0
    _save_state_locked()
    _set_status(v1, chat_id, force=True)

    try:
        # Safety bounds.
        count = int(st.settings.num_clips or 1)
        if count < 1:
            count = 1
        if count > 20:
            count = 20

        def _progress(prefix: str, msg: str) -> None:
            p = str(prefix).upper().strip()
            if p in {"AO"}:
                _set_status(v1, chat_id, stage="Pick", detail=msg)
            elif p in {"IMAGE2"}:
                _set_status(v1, chat_id, stage="Image", detail=msg)
            elif p in {"VIDEO2"}:
                _set_status(v1, chat_id, stage="Video", detail=msg)

        for i in range(1, count + 1):
            _set_status(v1, chat_id, stage="Pick", detail=f"Clip {i}/{count}", force=True)
            res = generate_one_anomalie_objet(log_fn=_progress)

            clip_id = f"ao_{uuid.uuid4().hex[:10]}"
            hook_title = str(getattr(res, "hook_title", "") or "").strip() or clip_id
            hashtags = list(getattr(res, "hashtags", []) or [])
            snap_hook = str(getattr(res, "snap_hook", "") or "").strip()

            clip = AOClip(
                clip_id=clip_id,
                video_path=res.video_path,
                hook_title=hook_title,
                hashtags=hashtags,
                snap_hook=snap_hook,
                status="pending",
                index=i,
                total=count,
            )

            st.clips[clip_id] = clip
            st.ordered_ids.append(clip_id)
            _normalize_state(st)
            _save_state_locked()
            _send_clip(v1, chat_id, clip)

        _set_status(v1, chat_id, stage="Done", detail=f"Generated {count} clip(s)", force=True)

    except Exception as e:
        try:
            v1._tg_api("sendMessage", data={"chat_id": str(chat_id), "text": f"V2 generation failed: {e}"})
        except Exception:
            pass
    finally:
        st.generating = False
        st.stage = ""
        st.detail = ""
        _save_state_locked()
        try:
            _ao_menu(v1, chat_id, force_new=False)
        except Exception:
            pass


def install(v1_module) -> None:
    """Patch V1 telegram_control module in-memory to add V2 entry points."""

    v1 = v1_module

    # Load persisted integration state.
    with STATE_LOCK:
        _load_state()

    orig_main_menu = v1._main_menu
    orig_handle_callback = v1._handle_callback

    # One-time cleanup/migration: earlier versions injected V2 clips into V1 state.
    # To restore strict isolation, move those clips into V2 state and remove them from V1.
    try:
        with v1.STATE_LOCK:
            v1._load_state()
            moved = 0
            for chat_id, v1st in list(getattr(v1, "_CHAT", {}).items()):
                aost = _st(int(chat_id))
                for cid, c in list(getattr(v1st, "clips", {}).items()):
                    if not str(cid).startswith("ao_"):
                        continue
                    try:
                        vp = Path(str(getattr(c, "video_path", "") or "")).resolve()
                    except Exception:
                        continue
                    if vp != AO_VIDEOS_DIR and AO_VIDEOS_DIR not in vp.parents:
                        continue

                    aost.clips[str(cid)] = AOClip(
                        clip_id=str(cid),
                        video_path=str(getattr(c, "video_path", "")),
                        hook_title=str(getattr(c, "hook_title", "") or "").strip(),
                        hashtags=[str(x) for x in (getattr(c, "hashtags", []) or []) if str(x).strip()],
                        snap_hook=str(getattr(c, "snap_hook", "") or "").strip(),
                        status=str(getattr(c, "status", "pending") or "pending"),
                        message_chat_id=getattr(c, "message_chat_id", None),
                        message_id=getattr(c, "message_id", None),
                        index=int(getattr(c, "index", 0) or 0),
                        total=int(getattr(c, "total", 0) or 0),
                    )
                    if str(cid) not in aost.ordered_ids:
                        aost.ordered_ids.append(str(cid))
                    moved += 1

                    # Remove from V1.
                    try:
                        v1st.clips.pop(cid, None)
                        v1st.ordered_ids = [x for x in v1st.ordered_ids if x != cid]
                        v1st.approved_ids = [x for x in v1st.approved_ids if x != cid]
                        v1st.rejected_ids = [x for x in v1st.rejected_ids if x != cid]
                    except Exception:
                        pass

                _normalize_state(aost)

            if moved:
                try:
                    for _, v1st in list(getattr(v1, "_CHAT", {}).items()):
                        try:
                            v1._normalize_state(v1st)
                        except Exception:
                            pass
                    v1._save_state_locked()
                except Exception:
                    pass
                _save_state_locked()
    except Exception:
        pass

    def patched_main_menu(chat_id: int, *, force_new: bool = False):
        # Rebuild the same menu as V1 but with an extra V2 entry.
        st = v1._state(chat_id)
        st.active_menu = "main"
        v1._save_state_locked()

        text = "Control panel"
        if getattr(st, "generating", False):
            text = v1._format_generation_status(st)

        reply_markup = v1._kb(
            [
                [v1._btn("▶️ Launch generation", "menu:launch")],
                [v1._btn("🧩 V2: Anomalie – Objet", "v2:ao:menu")],
                [v1._btn("⚙️ Settings", "menu:settings")],
                [v1._btn("📋 Queue", "menu:queue")],
                [v1._btn("✅ Approved clips", "menu:approved")],
                [v1._btn("❌ Rejected clips", "menu:rejected")],
            ]
        )
        v1._send_or_edit_panel(chat_id, text, reply_markup, force_new=force_new)

    def patched_handle_callback(chat_id: int, data: str):
        if data == "v2:ao:menu":
            _ao_menu(v1, chat_id, force_new=False)
            return
        if data.startswith("v2:ao:setcount:"):
            try:
                n = int(data.split(":")[-1])
            except Exception:
                n = 3
            if n < 1:
                n = 1
            if n > 20:
                n = 20
            st = _st(chat_id)
            st.settings.num_clips = n
            _save_state_locked()
            _ao_menu(v1, chat_id)
            return
        if data == "v2:ao:launch":
            st = _st(chat_id)
            if st.generating:
                return
            t = threading.Thread(target=_generate_thread, args=(v1, chat_id), daemon=True)
            t.start()
            _ao_menu(v1, chat_id, force_new=False)
            return

        if data == "v2:ao:queue":
            _list_menu(v1, chat_id, "queue")
            return
        if data == "v2:ao:approved":
            _list_menu(v1, chat_id, "approved")
            return
        if data == "v2:ao:rejected":
            _list_menu(v1, chat_id, "rejected")
            return

        if data.startswith("v2:ao:clip:open:"):
            cid = data.split(":")[-1]
            clip = _st(chat_id).clips.get(cid)
            if clip:
                _send_clip(v1, chat_id, clip)
            return

        if data.startswith("v2:ao:clip:copytext:"):
            cid = data.split(":")[-1]
            clip = _st(chat_id).clips.get(cid)
            if clip:
                text = build_publish_text_from_clip(clip, platform="snap")
                try:
                    v1._tg_api("sendMessage", data={"chat_id": str(chat_id), "text": text})
                except Exception:
                    pass
            return

        if data.startswith("v2:ao:clip:approve:"):
            cid = data.split(":")[-1]
            st = _st(chat_id)
            clip = st.clips.get(cid)
            if clip:
                clip.status = "approved"
                _normalize_state(st)
                _save_state_locked()
                _edit_clip_message(v1, chat_id, clip)
                _ao_menu(v1, chat_id)
                _advance_after_action(v1, chat_id, cid)
            return

        if data.startswith("v2:ao:clip:reject:"):
            cid = data.split(":")[-1]
            st = _st(chat_id)
            clip = st.clips.get(cid)
            if clip:
                clip.status = "rejected"
                _normalize_state(st)
                _save_state_locked()
                _edit_clip_message(v1, chat_id, clip)
                _ao_menu(v1, chat_id)
                _advance_after_action(v1, chat_id, cid)
            return

        if data.startswith("v2:ao:clip:later:"):
            cid = data.split(":")[-1]
            st = _st(chat_id)
            clip = st.clips.get(cid)
            if clip:
                clip.status = "pending"
            if cid in st.ordered_ids:
                st.ordered_ids = [x for x in st.ordered_ids if x != cid]
                st.ordered_ids.append(cid)
            _normalize_state(st)
            _save_state_locked()
            if clip:
                _edit_clip_message(v1, chat_id, clip)
            _ao_menu(v1, chat_id)
            _advance_after_action(v1, chat_id, cid)
            return

        if data.startswith("v2:ao:clip:delete:"):
            cid = data.split(":")[-1]
            _delete_clip_and_open_next(v1, chat_id, cid)
            return

        return orig_handle_callback(chat_id, data)

    v1._main_menu = patched_main_menu
    v1._handle_callback = patched_handle_callback

    # Also patch startup panel creation so V2 button appears immediately.
    v1._log("TELEGRAM", "Installed V2 format integration: anomalie_objet")
