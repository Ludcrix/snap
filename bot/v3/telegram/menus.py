from __future__ import annotations

from ..state import V3State, VideoItem


def _btn(text: str, cb: str) -> dict:
    return {"text": text, "callback_data": cb}


def _kb(rows: list[list[dict]]) -> dict:
    return {"inline_keyboard": rows}


def render_home(st: V3State) -> tuple[str, dict]:
    active = st.active_session_id or "(stopped)"
    paused = bool(getattr(st, "session_paused", False))
    pending = sum(1 for v in st.videos.values() if v.status == "pending")
    approved = sum(1 for v in st.videos.values() if v.status == "approved")
    rejected = sum(1 for v in st.videos.values() if v.status == "rejected")
    dev = (st.device_status or "?")
    s = getattr(st, "settings", {})
    s = dict(s) if isinstance(s, dict) else {}
    adb_on = bool(s.get("device_input_enabled", False))

    status_line = "paused" if (st.active_session_id and paused) else ("running" if st.active_session_id else "stopped")
    text = (
        "V3 — Mobile Agent (SIMULÉ; actions visibles via ADB si activées)\n"
        f"Session: {active} ({status_line})\n"
        f"Device: {dev} | ADB input: {'ON' if adb_on else 'OFF'}\n"
        f"Pending: {pending} | Approved: {approved} | Rejected: {rejected}\n"
        "\n"
        "Règle: aucune action auto de publication.\n"
        "Les actions type Like sont SIMULÉES (aucune automatisation)."
    )

    sess_row = [_btn("▶️ Démarrer session", "v3:sess:start"), _btn("⏹ Stop", "v3:sess:stop")]
    if st.active_session_id:
        if paused:
            sess_row.insert(1, _btn("▶️ Reprendre", "v3:sess:resume"))
        else:
            sess_row.insert(1, _btn("⏸ Pause", "v3:sess:pause"))

    kb = _kb(
        [
            sess_row,
            [_btn("👀 Step (scroll+observe)", "v3:sess:step")],
            [_btn("⚙️ Réglages", "v3:settings")],
            [_btn("🗑 Supprimer toutes les vidéos", "v3:videos:purge")],
            [_btn("📥 Voir pending", "v3:list:pending")],
            [_btn("✅ Voir approved", "v3:list:approved"), _btn("❌ Voir rejected", "v3:list:rejected")],
        ]
    )
    return text, kb


def render_settings(st: V3State) -> tuple[str, dict]:
    s = getattr(st, "settings", {})
    s = dict(s) if isinstance(s, dict) else {}

    def _f(key: str, default: float) -> float:
        try:
            return float(s.get(key, default))
        except Exception:
            return float(default)

    def _i(key: str, default: int) -> int:
        try:
            return int(s.get(key, default))
        except Exception:
            return int(default)

    # Shared fields
    adb_on = bool(s.get("device_input_enabled", False))
    risk_safe = bool(s.get("risk_safety_enabled", True))
    pause_s = _f("scroll_pause_seconds", 0.8)
    loop_s = _f("loop_sleep_seconds", 2.0)
    tgt = _i("target_sent_per_session", 10)
    stop_reason = str(getattr(st, "last_session_stop_reason", "") or "").strip()

    text = (
        "V3 — Réglages (UI v3.1)\n"
        "\n"
        f"ADB input (scroll visible): {'ON' if adb_on else 'OFF'}\n"
        f"Sécurité Risk (auto-stop HIGH_RISK): {'ON' if risk_safe else 'OFF'}\n"
        "\n"
        f"Pause entre scrolls: {pause_s:.2f}s\n"
        f"Vitesse boucle (sleep): {loop_s:.2f}s\n"
        "\n"
        f"Stop après N vidéos envoyées TG: {tgt}\n"
        f"Dernier arrêt: {stop_reason if stop_reason else '(none)'}\n"
        "\n"
        "Choisis une page:\n"
        "- Score historique\n"
        "- Viral/Latent"
    ).strip()

    kb = _kb(
        [
            [_btn(("✅ ADB input: ON" if adb_on else "☑️ ADB input: OFF"), "v3:toggle:device_input")],
            [_btn(("🛡️ Sécurité Risk: ON" if risk_safe else "⚠️ Sécurité Risk: OFF"), "v3:toggle:risk_safety")],
            [_btn("📈 Score historique", "v3:settings:legacy"), _btn("🔥/💎 Viral+Latent", "v3:settings:virality")],
            [_btn("Pause -", "v3:set:pause:-"), _btn("Pause +", "v3:set:pause:+")],
            [_btn("Sleep -", "v3:set:sleep:-"), _btn("Sleep +", "v3:set:sleep:+")],
            [_btn("Target -", "v3:set:tgt:-"), _btn("Target +", "v3:set:tgt:+")],
            [_btn("🔄 Reset réglages (origine)", "v3:settings:reset")],
            [_btn("⬅️ Retour", "v3:home")],
        ]
    )
    return text, kb


def render_settings_legacy(st: V3State) -> tuple[str, dict]:
    s = getattr(st, "settings", {})
    s = dict(s) if isinstance(s, dict) else {}

    def _f(key: str, default: float) -> float:
        try:
            return float(s.get(key, default))
        except Exception:
            return float(default)

    thr = _f("score_threshold", 0.65)
    w_b = _f("weight_banalite", 0.35)
    w_p = _f("weight_potentiel_viral", 0.35)
    w_r = _f("weight_rythme", 0.30)
    rt = _f("rythme_target", 0.45)

    text = (
        "V3 — Réglages: Score historique\n"
        "\n"
        f"Threshold: {thr:.2f}\n"
        f"Weights: w_b={w_b:.2f}  w_p={w_p:.2f}  w_r={w_r:.2f}\n"
        f"Rythme target: {rt:.2f}"
    ).strip()

    kb = _kb(
        [
            [_btn("Thr -", "v3:set:thr:-"), _btn("Thr +", "v3:set:thr:+")],
            [_btn("w_b -", "v3:set:wb:-"), _btn("w_b +", "v3:set:wb:+")],
            [_btn("w_p -", "v3:set:wp:-"), _btn("w_p +", "v3:set:wp:+")],
            [_btn("w_r -", "v3:set:wr:-"), _btn("w_r +", "v3:set:wr:+")],
            [_btn("Ryt -", "v3:set:rt:-"), _btn("Ryt +", "v3:set:rt:+")],
            [_btn("🔄 Reset réglages (origine)", "v3:settings:reset")],
            [_btn("⬅️ Retour réglages", "v3:settings")],
        ]
    )
    return text, kb


def render_settings_virality(st: V3State) -> tuple[str, dict]:
    s = getattr(st, "settings", {})
    s = dict(s) if isinstance(s, dict) else {}

    def _f(key: str, default: float) -> float:
        try:
            return float(s.get(key, default))
        except Exception:
            return float(default)

    thr_v = _f("threshold_viral", 0.72)
    v_wb = _f("viral_w_banalite", 0.30)
    v_wp = _f("viral_w_potentiel_viral", 0.45)
    v_wr = _f("viral_w_rythme", 0.25)
    v_rt = _f("viral_rythme_target", 0.50)

    thr_l = _f("threshold_latent", 0.60)
    l_wb = _f("latent_w_banalite", 0.45)
    l_wp = _f("latent_w_potentiel_viral", 0.20)
    l_wr = _f("latent_w_rythme", 0.35)
    l_rt = _f("latent_rythme_target", 0.38)

    # STV / OCR tuning (used by STV refresh button)
    try:
        stv_tries = int(s.get("stv_ocr_tries", 3) or 3)
    except Exception:
        stv_tries = 3
    stv_rx0 = _f("stv_right_crop_x0_ratio", 0.72)
    stv_ry0 = _f("stv_right_crop_y0_ratio", 0.55)
    stv_ry1 = _f("stv_right_crop_y1_ratio", 0.97)
    stv_vr = _f("stv_max_views_like_ratio", 500.0)
    try:
        stv_vm = int(s.get("stv_abs_max_views", 200_000_000) or 200_000_000)
    except Exception:
        stv_vm = 200_000_000

    text = (
        "V3 — Réglages: Viral/Latent\n"
        "\n"
        "(🔥 Viral déjà actif)\n"
        f"Thr_viral: {thr_v:.2f} | w_b={v_wb:.2f} w_p={v_wp:.2f} w_r={v_wr:.2f} | target={v_rt:.2f}\n"
        "\n"
        "(💎 Viral latent)\n"
        f"Thr_latent: {thr_l:.2f} | w_b={l_wb:.2f} w_p={l_wp:.2f} w_r={l_wr:.2f} | target={l_rt:.2f}\n"
        "\n"
        "(🧮 STV / OCR)\n"
        f"OCR tries: {stv_tries} | right_crop x0={stv_rx0:.2f} y0={stv_ry0:.2f} y1={stv_ry1:.2f}\n"
        f"Sanity: max_views/like={stv_vr:.0f} | abs_max_views={stv_vm:,}".replace(",", " ")
    ).strip()

    kb = _kb(
        [
            [_btn("ThrV -", "v3:set:tv:-"), _btn("ThrV +", "v3:set:tv:+")],
            [_btn("Vw_b -", "v3:set:vwb:-"), _btn("Vw_b +", "v3:set:vwb:+")],
            [_btn("Vw_p -", "v3:set:vwp:-"), _btn("Vw_p +", "v3:set:vwp:+")],
            [_btn("Vw_r -", "v3:set:vwr:-"), _btn("Vw_r +", "v3:set:vwr:+")],
            [_btn("Vtgt -", "v3:set:vrt:-"), _btn("Vtgt +", "v3:set:vrt:+")],
            [_btn("ThrL -", "v3:set:tl:-"), _btn("ThrL +", "v3:set:tl:+")],
            [_btn("Lw_b -", "v3:set:lwb:-"), _btn("Lw_b +", "v3:set:lwb:+")],
            [_btn("Lw_p -", "v3:set:lwp:-"), _btn("Lw_p +", "v3:set:lwp:+")],
            [_btn("Lw_r -", "v3:set:lwr:-"), _btn("Lw_r +", "v3:set:lwr:+")],
            [_btn("Ltgt -", "v3:set:lrt:-"), _btn("Ltgt +", "v3:set:lrt:+")],
            [_btn("OCR -", "v3:set:ocr:-"), _btn("OCR +", "v3:set:ocr:+")],
            [_btn("CropX -", "v3:set:rx0:-"), _btn("CropX +", "v3:set:rx0:+")],
            [_btn("CropY -", "v3:set:ry0:-"), _btn("CropY +", "v3:set:ry0:+")],
            [_btn("CropY1 -", "v3:set:ry1:-"), _btn("CropY1 +", "v3:set:ry1:+")],
            [_btn("ViewsRatio -", "v3:set:vr:-"), _btn("ViewsRatio +", "v3:set:vr:+")],
            [_btn("AbsViews -", "v3:set:vm:-"), _btn("AbsViews +", "v3:set:vm:+")],
            [_btn("🎯 Apprendre clic âge", "v3:stv:teach")],
            [_btn("🔄 Reset réglages (origine)", "v3:settings:reset")],
            [_btn("⬅️ Retour réglages", "v3:settings")],
        ]
    )
    return text, kb


def render_list(st: V3State, which: str, *, page: int = 0) -> tuple[str, dict]:
    which = which.strip().lower()
    if which not in {"pending", "approved", "rejected"}:
        which = "pending"

    items = [v for v in st.videos.values() if v.status == which]
    items.sort(key=lambda v: float(v.timestamp), reverse=True)

    page_size = 10
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    try:
        page_i = int(page)
    except Exception:
        page_i = 0
    page_i = max(0, min(total_pages - 1, page_i))

    start = page_i * page_size
    end = start + page_size
    shown = items[start:end]

    lines: list[str] = [f"V3 — {which.upper()}", f"Count: {total}", f"Page: {page_i + 1}/{total_pages}", ""]
    for idx, v in enumerate(shown, start=1):
        url = v.source_url or "(url inconnue)"
        lines.append(f"{idx}. {v.internal_id}  score={v.score:.2f}")
        lines.append(f"   {url}")

    rows: list[list[dict]] = []
    for v in shown:
        rows.append([_btn("🎬 Ouvrir", f"v3:item:open:{v.internal_id}")])

    nav: list[dict] = []
    if page_i > 0:
        nav.append(_btn("⬅️ Précédent", f"v3:list:{which}:{page_i - 1}"))
    if page_i + 1 < total_pages:
        nav.append(_btn("Suivant ➡️", f"v3:list:{which}:{page_i + 1}"))
    if nav:
        rows.append(nav)

    rows.append([_btn("⬅️ Retour", "v3:home")])
    return "\n".join(lines).strip(), _kb(rows)


def _format_item(v: VideoItem) -> str:
    url = v.source_url or "(url inconnue)"
    text = (
        "V3 — Video\n"
        f"ID: {v.internal_id}\n"
        f"Source: {v.source}\n"
        f"URL: {url}\n"
        f"Score: {v.score:.2f} (seuil {float(getattr(v, 'threshold', 0.0) or 0.0):.2f})\n"
        f"Raison: {str(getattr(v, 'reason', '') or '').strip()}\n"
        f"Status: {v.status}\n"
        f"Session: {v.session_id}\n"
        f"Timestamp: {v.timestamp}\n"
        f"Titre: {str(getattr(v, 'title', '') or '').strip()}\n"
        f"Hashtags: {' '.join(getattr(v, 'hashtags', []) or [])}\n"
    )

    # --- STRICTLY ADDITIVE TEMPORAL ANALYSIS (display-only) ---
    # Applies to already existing items when opened in Telegram.
    try:
        status = str(getattr(v, "status", "") or "").strip().lower()
        is_retained = status in {"pending", "approved"}
    except Exception:
        is_retained = False

    if is_retained:
        try:
            from ..temporal_analysis import analyze_from_meta, format_telegram_block

            meta = getattr(v, "meta", None)
            meta = dict(meta) if isinstance(meta, dict) else {}
            block = format_telegram_block(analyze_from_meta(meta=meta))
            if str(block or "").strip():
                text = text.rstrip() + "\n\n" + block
        except Exception:
            pass

    return text


def render_item(v: VideoItem) -> tuple[str, dict]:
    rows: list[list[dict]] = []

    if v.status == "pending":
        rows.append([_btn("✅ Approuver", f"v3:item:approve:{v.internal_id}"), _btn("❌ Rejeter", f"v3:item:reject:{v.internal_id}")])
        rows.append([_btn("🗑 Supprimer", f"v3:item:delete:{v.internal_id}")])
        rows.append([_btn("🧮 Calculer STV", f"v3:item:stv:{v.internal_id}")])
    elif v.status == "approved":
        # Like is only available AFTER approval (simulated-only).
        rows.append([_btn("❤️ Like (simulé)", f"v3:item:like:{v.internal_id}")])
        rows.append([_btn("🗑 Supprimer", f"v3:item:delete:{v.internal_id}")])
        rows.append([_btn("🧮 Calculer STV", f"v3:item:stv:{v.internal_id}")])
    else:
        rows.append([_btn("🗑 Supprimer", f"v3:item:delete:{v.internal_id}")])

    rows.append([_btn("⬅️ Retour", "v3:home")])
    return _format_item(v), _kb(rows)
