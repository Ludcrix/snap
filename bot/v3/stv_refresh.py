from __future__ import annotations

import io
import os
import shutil
import subprocess
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any
import json

from .temporal_analysis import analyze_from_meta, format_telegram_block, extract_metrics_from_ocr_text, parse_relative_pub_time
from .stv_age_api import try_fetch_age_seconds


@dataclass(frozen=True)
class StvRefreshResult:
    ok: bool
    telegram_block: str
    debug: str
    raw_ocr_text: str = ""


def _adb_base(*, android_agent: Any | None = None) -> list[str]:
    adb_path = None
    serial = None

    try:
        if android_agent is not None:
            cfg = getattr(android_agent, "_cfg", None)
            adb_path = getattr(cfg, "adb_path", None)
            serial = getattr(cfg, "serial", None)
    except Exception:
        adb_path = None
        serial = None

    adb = str(os.getenv("ADB_PATH", "")).strip() or str(adb_path or "adb").strip() or "adb"
    base = [adb]
    if serial:
        base += ["-s", str(serial)]
    return base


def _run(cmd: list[str], *, timeout_s: float) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(cmd, capture_output=True, timeout=float(timeout_s))


def _run_ok(cmd: list[str], *, timeout_s: float) -> bool:
    cp = _run(cmd, timeout_s=timeout_s)
    return int(cp.returncode) == 0


def _exec_out_bytes(cmd: list[str], *, timeout_s: float) -> bytes:
    cp = _run(cmd, timeout_s=timeout_s)
    if int(cp.returncode) != 0:
        raise RuntimeError(f"cmd_failed rc={cp.returncode} cmd={cmd} stderr={cp.stderr[:200]!r}")
    return cp.stdout or b""


def _open_url_in_instagram(url: str, *, android_agent: Any | None = None) -> bool:
    base = _adb_base(android_agent=android_agent)
    # Best-effort VIEW intent targeting Instagram.
    cmd = base + [
        "shell",
        "am",
        "start",
        "-a",
        "android.intent.action.VIEW",
        "-d",
        str(url),
        "-p",
        "com.instagram.android",
    ]
    return _run_ok(cmd, timeout_s=7.0)


def _load_stv_click() -> dict | None:
    try:
        p = Path("storage/v3/stv_click.json")
        if not p.exists():
            return None
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def _crop_png_bytes(png: bytes, box: tuple[int, int, int, int]) -> bytes:
    try:
        from PIL import Image
        import io as _io

        img = Image.open(_io.BytesIO(png))
        cropped = img.crop(box)
        out = _io.BytesIO()
        cropped.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return png


def _screenshot_png_bytes(*, android_agent: Any | None = None) -> bytes:
    base = _adb_base(android_agent=android_agent)
    cmd = base + ["exec-out", "screencap", "-p"]
    out = _exec_out_bytes(cmd, timeout_s=7.0)
    if not out:
        raise RuntimeError("empty_screencap")
    return out


def _ocr_text_from_png(
    png: bytes,
    *,
    right_crop_x0_ratio: float = 0.72,
    right_crop_y0_ratio: float = 0.55,
    right_crop_y1_ratio: float = 0.97,
) -> str:
    # Optional dependencies: do not hard-require.
    try:
        from PIL import Image  # type: ignore
    except Exception as e:
        raise RuntimeError(f"PIL_missing:{type(e).__name__}")

    try:
        import pytesseract  # type: ignore
    except Exception as e:
        raise RuntimeError(f"pytesseract_missing:{type(e).__name__}")

    # Configure Tesseract binary.
    tcmd = str(os.getenv("TESSERACT_CMD", "")).strip()
    if not tcmd:
        # Try PATH first.
        tcmd = str(shutil.which("tesseract") or "").strip()

    if not tcmd and os.name == "nt":
        candidates = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]
        for p in candidates:
            if os.path.exists(p):
                tcmd = p
                break

    # If still not found on Windows, do a shallow scan of Program Files roots.
    if not tcmd and os.name == "nt":
        def _scan_roots():
            roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
            for root in roots:
                if not root:
                    continue
                root = str(root)
                if not os.path.isdir(root):
                    continue
                max_depth = 3
                for dirpath, dirnames, filenames in os.walk(root):
                    rel = os.path.relpath(dirpath, root)
                    depth = 0 if rel == '.' else rel.count(os.sep) + 1
                    if 'tesseract.exe' in (f.lower() for f in filenames):
                        return os.path.join(dirpath, 'tesseract.exe')
                    if depth >= max_depth:
                        dirnames[:] = []
            return None
        found = _scan_roots()
        if found:
            tcmd = found

    if tcmd:
        try:
            pytesseract.pytesseract.tesseract_cmd = tcmd
        except Exception:
            pass

    img = Image.open(io.BytesIO(png))

    # IG counters are often in the lower third AND/or on the right icon column.
    # Do targeted OCR passes to improve extraction, but keep full-image OCR too.
    try:
        w, h = img.size
    except Exception:
        w, h = 0, 0

    def _ocr(im: Image.Image) -> str:
        # Try FRA+ENG if available; fallback to default.
        # PSM 6 tends to work better for UI text blocks.
        cfg = "--psm 6"
        try:
            return str(pytesseract.image_to_string(im, lang="fra+eng", config=cfg) or "")
        except Exception:
            try:
                return str(pytesseract.image_to_string(im, config=cfg) or "")
            except Exception:
                return str(pytesseract.image_to_string(im) or "")

    full_text = _ocr(img)

    bottom_text = ""
    if w > 0 and h > 0:
        try:
            y0 = int(h * (2.0 / 3.0))
            bottom = img.crop((0, y0, w, h))
            bottom_text = _ocr(bottom)
        except Exception:
            bottom_text = ""

    right_text = ""
    if w > 0 and h > 0:
        try:
            # Right-side action column (icons + counters) is typically in the
            # right side and often in the lower half. Make ratios tunable.

            x0 = int(w * max(0.0, min(0.95, float(right_crop_x0_ratio))))
            y0 = int(h * max(0.0, min(0.95, float(right_crop_y0_ratio))))
            y1 = int(h * max(0.05, min(1.0, float(right_crop_y1_ratio))))
            right = img.crop((x0, y0, w, y1))
            right_text = _ocr(right)
        except Exception:
            right_text = ""

    # Return combined text so downstream parsers can use all sources.
    # Use explicit tags so the extractor can apply source-specific heuristics.
    parts: list[str] = []
    if full_text and full_text.strip():
        parts.append(full_text.strip())
    if right_text and right_text.strip():
        parts.append("[OCR_RIGHT_COLUMN]\n" + right_text.strip())
    if bottom_text and bottom_text.strip():
        parts.append("[OCR_BOTTOM]\n" + bottom_text.strip())
    return "\n\n".join(parts).strip()


def strip_existing_stv_block(text: str) -> str:
    # Strip existing appended temporal block (ours) if present.
    marker = "\n\n📅 Publiée :"
    i = str(text or "").find(marker)
    if i >= 0:
        return str(text or "")[:i].rstrip()
    return str(text or "").rstrip()


def refresh_stv_from_url(url: str, *, android_agent: Any | None = None, settings: dict | None = None) -> StvRefreshResult:
    """Compute a fresh STV block from URL (ADB -> screenshot -> OCR -> STV).

    Strictly additive: does not persist anything; used for Telegram refresh button.
    """
    t0 = time.time()
    dbg: list[str] = []

    s = dict(settings or {})

    debug_android = str(os.getenv("V3_ANDROID_DEBUG", "")).strip() == "1"

    def _sf(name: str, default: float) -> float:
        try:
            if name in s:
                return float(s.get(name))
        except Exception:
            pass
        try:
            return float(str(os.getenv(name.upper(), "")).strip() or default)
        except Exception:
            return float(default)

    def _si(name: str, default: int) -> int:
        try:
            if name in s:
                return int(s.get(name))
        except Exception:
            pass
        try:
            return int(str(os.getenv(name.upper(), "")).strip() or default)
        except Exception:
            return int(default)

    ocr_tries = _si("stv_ocr_tries", 3)
    ocr_tries = max(1, min(5, int(ocr_tries)))

    right_x0 = _sf("stv_right_crop_x0_ratio", 0.72)
    right_y0 = _sf("stv_right_crop_y0_ratio", 0.55)
    right_y1 = _sf("stv_right_crop_y1_ratio", 0.97)

    max_views_like_ratio = _sf("stv_max_views_like_ratio", 500.0)
    abs_max_views = _si("stv_abs_max_views", 200_000_000)

    print(f"[STV] refresh start url={url!r}", flush=True)

    # Optional: try a reliable "age" source before OCR.
    age_api = None
    try:
        age_api = try_fetch_age_seconds(url)
    except Exception:
        age_api = None

    if age_api is not None:
        dbg.append(f"{age_api.source}={age_api.age_seconds}")
        if debug_android:
            print(f"[STV] age_api ok age_seconds={age_api.age_seconds} source={age_api.source}", flush=True)
    else:
        dbg.append("age_api=none")

    ok_open = False
    try:
        ok_open = _open_url_in_instagram(url, android_agent=android_agent)
    except Exception as e:
        dbg.append(f"open_exc={type(e).__name__}")
    dbg.append(f"open_ok={ok_open}")

    if not ok_open:
        msg = "/!\\ STV: impossible d’ouvrir l’URL sur le device (adb am start)."
        print(f"[STV] refresh fail reason=open_url {dbg}", flush=True)
        return StvRefreshResult(False, telegram_block=msg, debug=";".join(dbg))

    # Let UI settle.
    try:
        time.sleep(1.2)
    except Exception:
        pass

    # If a learned click exists, perform the tap to reveal the age overlay before screenshots.
    learned = _load_stv_click()
    focused_click = None
    if learned:
        try:
            x_px = int(learned.get("x_px") or 0)
            y_px = int(learned.get("y_px") or 0)
            sw = int(learned.get("screen_w") or 0)
            sh = int(learned.get("screen_h") or 0)
            # Prefer AndroidAgent tap if available.
            tapped = False
            if android_agent is not None and hasattr(android_agent, "tap"):
                try:
                    android_agent.tap(x_px, y_px)
                    tapped = True
                except Exception:
                    tapped = False
            if not tapped:
                try:
                    base = _adb_base(android_agent=android_agent)
                    try:
                        cp = subprocess.run(base + ["shell", "input", "tap", str(x_px), str(y_px)], capture_output=True, timeout=3.0)
                        if int(cp.returncode) == 0:
                            tapped = True
                            dbg.append(f"tap_cmd_ok rc=0 cmd={base + ['shell','input','tap',str(x_px),str(y_px)]}")
                        else:
                            tapped = False
                            try:
                                stderr_snip = (cp.stderr or b"")[:300]
                            except Exception:
                                stderr_snip = cp.stderr
                            dbg.append(f"tap_cmd_failed rc={cp.returncode} stderr={stderr_snip!r} cmd={base + ['shell','input','tap',str(x_px),str(y_px)]}")
                    except Exception as e:
                        tapped = False
                        dbg.append(f"tap_cmd_exc={type(e).__name__}:{e}")
                except Exception:
                    tapped = False
            if tapped:
                # small wait for UI to update
                try:
                    time.sleep(0.5)
                except Exception:
                    pass
                focused_click = {
                    "x_px": x_px,
                    "y_px": y_px,
                    "screen_w": sw,
                    "screen_h": sh,
                }
                if debug_android:
                    dbg.append(f"learned_click_tapped=1 x={x_px} y={y_px}")
        except Exception as e:
            if debug_android:
                dbg.append(f"learned_click_err={type(e).__name__}")

    # Multi-capture OCR: reduces flakiness (blur/motion/overlay variations).
    ocr_texts: list[str] = []
    metrics_list: list[dict[str, int | None]] = []
    best_age_text = ""
    best_age_minutes: float | None = None

    last_png: bytes | None = None
    for i in range(1, ocr_tries + 1):
        try:
            if i > 1:
                time.sleep(0.35)
        except Exception:
            pass

        try:
            png_i = _screenshot_png_bytes(android_agent=android_agent)
            last_png = png_i
            dbg.append(f"png_len_{i}={len(png_i)}")
        except Exception as e:
            dbg.append(f"screenshot_exc_{i}={type(e).__name__}")
            continue

        if debug_android:
            try:
                out_dir = Path("storage/v3/stv_debug")
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / f"stv_{int(time.time())}_try{i}.png"
                out_path.write_bytes(png_i)
                print(f"[STV] saved_png try={i} path={out_path.as_posix()}", flush=True)
            except Exception:
                pass

        try:
            # Prefer OCR on the learned click crop (if present) by prepending its text.
            ocr_parts: list[str] = []
            if focused_click is not None:
                try:
                    # Compute crop in actual screenshot coordinates.
                    try:
                        from PIL import Image
                        import io as _io

                        img = Image.open(_io.BytesIO(png_i))
                        w_img, h_img = img.size
                    except Exception:
                        w_img, h_img = 0, 0

                    # Use ratios if available in learned click, else fallback to px values.
                    cx = None
                    cy = None
                    try:
                        if focused_click.get("x_px") and focused_click.get("screen_w"):
                            cx = int(float(focused_click.get("x_px")) * (w_img / float(focused_click.get("screen_w") or 1)))
                        elif focused_click.get("x_px"):
                            cx = int(focused_click.get("x_px"))
                        elif focused_click.get("x_ratio"):
                            cx = int(float(focused_click.get("x_ratio")) * w_img)
                    except Exception:
                        cx = None
                    try:
                        if focused_click.get("y_px") and focused_click.get("screen_h"):
                            cy = int(float(focused_click.get("y_px")) * (h_img / float(focused_click.get("screen_h") or 1)))
                        elif focused_click.get("y_px"):
                            cy = int(focused_click.get("y_px"))
                        elif focused_click.get("y_ratio"):
                            cy = int(float(focused_click.get("y_ratio")) * h_img)
                    except Exception:
                        cy = None

                    if cx is not None and cy is not None and w_img > 0 and h_img > 0:
                        # default box size relative to image
                        w_box = int(max(80, w_img * 0.28))
                        h_box = int(max(40, h_img * 0.12))
                        x0 = max(0, cx - w_box // 2)
                        y0 = max(0, cy - h_box // 2)
                        x1 = min(w_img, x0 + w_box)
                        y1 = min(h_img, y0 + h_box)
                        try:
                            crop_bytes = _crop_png_bytes(png_i, (x0, y0, x1, y1))
                            crop_text = _ocr_text_from_png(
                                crop_bytes,
                                right_crop_x0_ratio=right_x0,
                                right_crop_y0_ratio=right_y0,
                                right_crop_y1_ratio=right_y1,
                            )
                            if crop_text and crop_text.strip():
                                ocr_parts.append("[OCR_CLICK_CROP]\n" + crop_text.strip())
                                if debug_android:
                                    dbg.append(f"click_crop_ok box={x0},{y0},{x1},{y1}")
                        except Exception:
                            pass
                except Exception:
                    pass

            # Always include full-image OCR as fallback.
            full_text = _ocr_text_from_png(
                png_i,
                right_crop_x0_ratio=right_x0,
                right_crop_y0_ratio=right_y0,
                right_crop_y1_ratio=right_y1,
            ) or ""
            if full_text and full_text.strip():
                ocr_parts.append(full_text.strip())
            ocr_i = "\n\n".join(ocr_parts)
            dbg.append(f"ocr_len_{i}={len(ocr_i)}")
        except Exception as e:
            dbg.append(f"ocr_exc_{i}={str(e)[:120]}")
            continue

        # Inject age override if available.
        if age_api is not None:
            ocr_i = f"AGE_SECONDS={int(age_api.age_seconds)}\n" + ocr_i

        ocr_texts.append(ocr_i)
        m_i = extract_metrics_from_ocr_text(ocr_i)
        metrics_list.append(m_i)

        # Pick an OCR text that yields an explicit age.
        if best_age_minutes is None and age_api is None:
            try:
                _, age_min_i, _ = parse_relative_pub_time(ocr_i, t_capture_utc=time_now_utc())
                if age_min_i is not None:
                    best_age_minutes = float(age_min_i)
                    best_age_text = ocr_i
            except Exception:
                pass

    if not ocr_texts and last_png is None:
        msg = "/!\\ STV: capture écran impossible (adb screencap)."
        print(f"[STV] refresh fail reason=screenshot {dbg}", flush=True)
        return StvRefreshResult(False, telegram_block=msg, debug=";".join(dbg))

    if not ocr_texts:
        msg = "STV: OCR indisponible (installer pillow + pytesseract + Tesseract)."
        print(f"[STV] refresh fail reason=ocr {dbg}", flush=True)
        return StvRefreshResult(False, telegram_block=msg, debug=";".join(dbg))

    # Aggregate metrics by robust vote (median of available values).
    def _median(vals: list[int]) -> int | None:
        if not vals:
            return None
        vals2 = sorted(int(v) for v in vals)
        mid = len(vals2) // 2
        if len(vals2) % 2 == 1:
            return vals2[mid]
        return int(round((vals2[mid - 1] + vals2[mid]) / 2.0))

    def _sanitize_metrics(m: dict[str, int | None]) -> tuple[dict[str, int | None], list[str]]:
        """Drop values that are very likely OCR mistakes.

        OCR sometimes reads a long phone-number-like string as views.
        We keep this conservative and mostly protect against absurd outliers.
        """
        reasons: list[str] = []
        out = dict(m)

        likes = out.get("likes")
        views = out.get("views")

        # Thresholds come from Telegram settings (preferred) or defaults.
        max_ratio = float(max_views_like_ratio or 500.0)
        abs_max = int(abs_max_views or 200_000_000)

        if isinstance(views, int):
            if views <= 0:
                out["views"] = None
                reasons.append("drop_views<=0")
            elif views > abs_max_views:
                out["views"] = None
                reasons.append(f"drop_views>abs_max({abs_max})")
            elif isinstance(likes, int) and likes > 0:
                # If views are wildly larger than likes, it's likely a concatenation OCR error.
                ratio = float(views) / float(likes)
                if ratio > max_ratio:
                    out["views"] = None
                    reasons.append(f"drop_views_ratio>{int(max_ratio)}")
                elif views < likes:
                    out["views"] = None
                    reasons.append("drop_views<likes")

        return out, reasons

    keys = ["likes", "comments", "sends", "saves", "remixes", "shares", "views"]
    voted: dict[str, int | None] = {}
    for k in keys:
        vals_k = [int(m.get(k)) for m in metrics_list if isinstance(m.get(k), int)]
        voted[k] = _median(vals_k)

    voted, drop_reasons = _sanitize_metrics(voted)
    if drop_reasons:
        dbg.append("sanitize=" + ",".join(drop_reasons))

    # Keep shares as an alias if only sends is present.
    if voted.get("shares") is None and isinstance(voted.get("sends"), int):
        voted["shares"] = voted.get("sends")
    # If shares is just an alias of sends, keep it for compute but don't force it as a separate signal.
    if isinstance(voted.get("shares"), int) and isinstance(voted.get("sends"), int) and voted.get("shares") == voted.get("sends"):
        dbg.append("shares_alias=sends")

    # Choose OCR text for age parsing.
    ocr_text = best_age_text or ocr_texts[0]

    metrics = voted
    dbg.append(
        "metrics="
        + ",".join(
            [
                f"likes={metrics.get('likes')}",
                f"comments={metrics.get('comments')}",
                f"sends={metrics.get('sends')}",
                f"saves={metrics.get('saves')}",
                f"remixes={metrics.get('remixes')}",
                f"shares={metrics.get('shares')}",
                f"views={metrics.get('views')}",
            ]
        )
    )

    # Compute analysis strictly from OCR-derived age + parsed counters
    meta = {
        "ocr_age_text": ocr_text,
        "ocr_metrics": metrics,
        "stv_refresh": True,
    }
    analysis = analyze_from_meta(meta=meta)
    block = format_telegram_block(analysis)

    # Print parsed age/time info for debugging when enabled.
    try:
        if debug_android:
            try:
                a_age = analysis.age_minutes
                a_tpub = analysis.t_pub_utc
                a_src = analysis.ocr_source
                a_raw = (analysis.ocr_pub_raw or "").replace("\n", " ")[:240]
                print(f"[STV] parsed_age age_min={a_age} t_pub={a_tpub} src={a_src} raw={a_raw}", flush=True)
            except Exception:
                pass
    except Exception:
        pass
    elapsed_ms = int((time.time() - t0) * 1000)
    dbg.append(f"elapsed_ms={elapsed_ms}")
    print(f"[STV] refresh done ok=True {dbg}", flush=True)

    return StvRefreshResult(True, telegram_block=block, debug=";".join(dbg), raw_ocr_text=ocr_text)


def time_now_utc():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
