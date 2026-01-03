from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformSpec:
    platform: str
    label: str
    deeplink: str
    help_text: str


_PLATFORM_SPECS: dict[str, PlatformSpec] = {
    "snap": PlatformSpec(
        platform="snap",
        label="Snap",
        # Telegram URL buttons do NOT allow custom schemes like "snapchat://".
        # Use an HTTPS universal link that opens the Snapchat app on mobile.
        deeplink="https://www.snapchat.com/",
        help_text="Snap ouvert → importer la vidéo → coller le texte → publier",
    ),
}


def get_platform_spec(platform: str) -> PlatformSpec:
    p = (platform or "").strip().lower()
    return _PLATFORM_SPECS.get(p) or PlatformSpec(
        platform=p or "unknown",
        label=(p or "App").capitalize(),
        deeplink="",
        help_text="Ouvrir l'app → importer la vidéo → coller le texte → publier",
    )


def normalize_hashtags(tags: list[str] | None, *, max_tags: int = 10) -> list[str]:
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
        if len(out) >= max_tags:
            break
    return out


def _stable_choice(options: list[str], key: str) -> str:
    if not options:
        return ""
    k = (key or "").encode("utf-8", errors="ignore")
    idx = sum(k) % len(options)
    return options[idx]


def ensure_publish_description(clip) -> str:
    """Return a short description line for publishing.

    Priority:
    1) clip.snap_hook if present
    2) clip.publish_desc if present
    3) deterministic fallback derived from title

    If clip.publish_desc exists and is empty, it will be set.
    """

    snap_hook = str(getattr(clip, "snap_hook", "") or "").strip()
    if snap_hook:
        return snap_hook

    existing = str(getattr(clip, "publish_desc", "") or "").strip()
    if existing:
        return existing

    title = str(getattr(clip, "hook_title", "") or "").strip()
    if not title:
        title = str(getattr(clip, "clip_id", "") or "").strip()

    templates = [
        "Tu as vu le détail ? 👀",
        "Regarde bien… quelque chose cloche.",
        "Ça te paraît normal ?",
        "Le détail est impossible.",
        "Une anomalie discrète, mais réelle.",
    ]
    desc = _stable_choice(templates, title)

    # Persist only if the clip supports the attribute.
    try:
        if hasattr(clip, "publish_desc"):
            setattr(clip, "publish_desc", desc)
    except Exception:
        pass
    return desc


def build_publish_text_from_clip(clip, *, platform: str = "snap") -> str:
    title = str(getattr(clip, "hook_title", "") or "").strip() or str(getattr(clip, "clip_id", "") or "").strip()
    desc = ensure_publish_description(clip)
    tags = normalize_hashtags(getattr(clip, "hashtags", []) or [])

    parts: list[str] = []
    parts.append(title)
    if desc:
        parts.append(desc)
    if tags:
        parts.append(" ".join(tags))
    return "\n".join(parts).strip()


def build_publish_caption(
    clip,
    *,
    platform: str = "snap",
    status_line: str | None = None,
    queue_line: str | None = None,
) -> str:
    """Build a Telegram-safe caption (plain text, no markdown).

    This is intended to be called at display-time only.
    """

    publish_text = build_publish_text_from_clip(clip, platform=platform)
    spec = get_platform_spec(platform)

    lines: list[str] = []
    lines.append(publish_text)
    lines.append("")
    if status_line:
        lines.append(status_line)
    if queue_line:
        lines.append(queue_line)

    # Help text (platform specific) – kept short to avoid caption limits.
    if spec.help_text:
        lines.append("")
        lines.append(spec.help_text)

    # Telegram video captions have a max length; keep a safety margin.
    text = "\n".join([ln for ln in lines if ln is not None]).strip()
    if len(text) > 950:
        text = text[:947] + "…"
    return text


def url_button(text: str, url: str) -> dict:
    return {"text": text, "url": url}


def handle_publish_snap(clip_id: str) -> tuple[str, dict | None]:
    """Reusable helper for Telegram publish assistance.

    This does NOT upload anything. It only provides a deep link and human instructions.
    Returns (help_text, reply_markup).
    """

    _ = clip_id  # reserved for future per-clip behavior
    spec = get_platform_spec("snap")
    if spec.deeplink:
        reply_markup = {"inline_keyboard": [[url_button("📤 Ouvrir Snapchat", spec.deeplink)]]}
    else:
        reply_markup = None
    return spec.help_text, reply_markup
