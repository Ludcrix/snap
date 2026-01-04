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
from .stv_age_api import try_fetch_age_with_selenium, try_fetch_age_from_html
from datetime import datetime, timezone


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


def refresh_stv_from_url(video, url, *args, **kwargs):
    """Open the reel on device, capture screenshots, OCR them, extract metrics and publication age.

    Returns `StvRefreshResult` with telegram_block when analysis succeeds, else a failure result.
    """
    print(f"[AGE][REFRESH] refresh start vid={getattr(video,'id',None)} url={url}", flush=True)
    logs: list[str] = []

    # 1) Try quick age API first (non-blocking)
    age_api_res = None
    try:
        from bot.v3.stv_age_api import fetch_created_time
        print(f"[AGE][REFRESH] calling fetch_created_time...", flush=True)
        age_api_res = fetch_created_time(url)
        print(f"[AGE][REFRESH] fetch_created_time returned: {age_api_res}", flush=True)
        if age_api_res is not None:
            logs.append(f"age_api={age_api_res}")
    except Exception as e:
        print(f"[AGE][REFRESH] fetch_created_time error: {type(e).__name__}: {e}", flush=True)
        logs.append(f"age_api_err={type(e).__name__}")

    # 2) If age API returned a complete result, try to compute analysis using only HTML-derived metrics later in integration.
    # But here we still try ADB/OCR to obtain metrics if needed.

    ocr_texts: list[str] = []
    try:
        # open url in Instagram app (best-effort)
        try:
            opened = _open_url_in_instagram(url, android_agent=kwargs.get("android_agent"))
            print(f"[STV] opened_in_instagram={opened}", flush=True)
        except Exception as e:
            print(f"[STV] open_url failed: {type(e).__name__}:{e}", flush=True)

        # capture screenshots and OCR them
        for i in range(3):
            try:
                time.sleep(0.8 if i == 0 else 0.6)
                png = _screenshot_png_bytes(android_agent=kwargs.get("android_agent"))
            except Exception as e:
                print(f"[STV] screenshot failed try={i} err={type(e).__name__}:{e}", flush=True)
                continue

            try:
                out_dir = Path("storage/v3/stv_debug")
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / f"stv_{int(time.time())}_try{i}.png"
                out_path.write_bytes(png)
                print(f"[STV] saved_png try={i} path={out_path.as_posix()} len={len(png)}", flush=True)
            except Exception as e:
                print(f"[STV] failed save_png try={i} err={type(e).__name__}:{e}", flush=True)

            try:
                txt = _ocr_text_from_png(png)
                print(f"[STV] OCR try={i} len={len(txt)} snippet={txt[:160]!r}", flush=True)
            except Exception as e:
                print(f"[STV] OCR exception try={i} err={type(e).__name__}:{e}", flush=True)
                txt = ""

            if txt and txt.strip():
                ocr_texts.append(txt)
                # parse pub time
                try:
                    t_pub, age_min, reason = parse_relative_pub_time(txt or "", t_capture_utc=datetime.now(timezone.utc))
                    print(f"[STV] parse_relative_pub_time try={i} age_min={age_min} reason={reason}", flush=True)
                    if age_min is not None:
                        meta = {"ocr_raw_text": txt}
                        try:
                            metrics = extract_metrics_from_ocr_text(txt)
                            if metrics:
                                meta["ocr_metrics"] = metrics
                        except Exception:
                            pass
                        try:
                            analysis = analyze_from_meta(meta=meta)
                            block = format_telegram_block(analysis)
                            debug_str = ";".join(logs + [f"ocr_try={i}"])
                            return StvRefreshResult(ok=True, telegram_block=block, debug=debug_str, raw_ocr_text=(txt or "")[:1000])
                        except Exception as e:
                            print(f"[STV] analyze_from_meta failed: {type(e).__name__}:{e}", flush=True)
                except Exception:
                    pass

        # If no age found, but we have OCRs, try aggregate analysis
        if ocr_texts:
            combined = "\n\n".join(ocr_texts)
            meta = {"ocr_raw_text": combined}
            try:
                metrics = extract_metrics_from_ocr_text(combined)
                if metrics:
                    meta["ocr_metrics"] = metrics
            except Exception:
                pass
            try:
                analysis = analyze_from_meta(meta=meta)
                block = format_telegram_block(analysis)
                debug_str = ";".join(logs + ["ocr_combined"])
                return StvRefreshResult(ok=True, telegram_block=block, debug=debug_str, raw_ocr_text=combined[:1000])
            except Exception as e:
                print(f"[STV] final analyze_from_meta failed: {type(e).__name__}:{e}", flush=True)
    except Exception as e:
        print(f"[STV] refresh flow exception: {type(e).__name__}:{e}", flush=True)

    debug_str = ";".join(logs)
    return StvRefreshResult(ok=False, telegram_block="", debug=debug_str, raw_ocr_text="")
