from __future__ import annotations

"""Standalone STV extraction probe (no Telegram).

Goal:
- When the tablet is currently showing a Reel, reproduce the STV pipeline end-to-end:
  1) Extract Reel URL via Instagram share-sheet (same as the bot)
  2) Open the URL in Instagram (adb am start)
  3) Screenshot (adb exec-out screencap)
  4) OCR (pytesseract + Tesseract)
  5) Compute STV block

Run (PowerShell):
  $env:V3_ADB_PATH = '<path to adb.exe>'
  $env:TESSERACT_CMD = 'C:\\Program Files\\Tesseract-OCR\\tesseract.exe'
  $env:V3_ANDROID_DEBUG = '1'
  C:/snap-bot/.venv/Scripts/python.exe -m bot.v3.stv_probe

Notes:
- This probe performs taps (share sheet) so it requires ADB input enabled.
- If you want to bypass URL extraction, provide --url.
"""

import argparse
import os
import shutil
import concurrent.futures
import traceback

from .android_agent import AndroidAgent, AndroidAgentConfig
from .stv_refresh import refresh_stv_from_url


def _make_agent(*, allow_input: bool) -> AndroidAgent:
    adb = str(os.getenv("V3_ADB_PATH", "")).strip() or str(os.getenv("ADB_PATH", "")).strip() or "adb"
    serial = str(os.getenv("V3_ANDROID_SERIAL", "")).strip() or None
    cfg = AndroidAgentConfig(adb_path=adb, serial=serial, allow_input=bool(allow_input))
    return AndroidAgent(cfg)


def _run_with_timeout(fn, timeout: float, *a, **kw):
    """
    Run fn(*a, **kw) in a thread and return (ok, result_or_exception)
    ok==True -> result returned; ok==False -> exception returned
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(fn, *a, **kw)
        try:
            res = fut.result(timeout=timeout)
            return True, res
        except Exception as e:
            try:
                exc = fut.exception(timeout=0)
            except Exception:
                exc = e
            return False, exc


def _find_tesseract_cmd() -> str | None:
    # 1) explicit env
    t = str(os.getenv("TESSERACT_CMD", "")).strip()
    if t:
        return t
    # 2) in PATH
    p = shutil.which("tesseract")
    if p:
        return p

    # 3) common Windows path(s)
    candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c

    # 4) try scanning ProgramFiles roots for tesseract.exe (shallow)
    def _scan_roots_for_tesseract():
        roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
        for root in roots:
            if not root:
                continue
            root = str(root)
            if not os.path.isdir(root):
                continue
            # walk but limit depth to avoid long scans
            max_depth = 3
            for dirpath, dirnames, filenames in os.walk(root):
                rel = os.path.relpath(dirpath, root)
                depth = 0 if rel == '.' else rel.count(os.sep) + 1
                if 'tesseract.exe' in (f.lower() for f in filenames):
                    return os.path.join(dirpath, 'tesseract.exe')
                if depth >= max_depth:
                    # do not recurse deeper than max_depth
                    dirnames[:] = []
        return None

    found = _scan_roots_for_tesseract()
    if found:
        return found

    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m bot.v3.stv_probe")
    p.add_argument("--url", default="", help="Instagram Reel URL to analyze (skip share-sheet extraction)")
    args = p.parse_args(argv)

    # Force verbose logs for this probe by default.
    if str(os.getenv("V3_ANDROID_DEBUG", "")).strip() != "1":
        os.environ["V3_ANDROID_DEBUG"] = "1"

    url = str(args.url or "").strip()

    print("[STV_PROBE] Step 0: init", flush=True)
    agent = _make_agent(allow_input=not bool(url))

    # QUICK AGE TAP FLOW if coordinates provided via env
    age_x = os.getenv("V3_AGE_TAP_X", "").strip()
    age_y = os.getenv("V3_AGE_TAP_Y", "").strip()
    if age_x and age_y:
        try:
            x = int(age_x)
            y = int(age_y)
        except Exception:
            print("[STV_PROBE] V3_AGE_TAP_X/Y must be integers", flush=True)
            return 5
        print(f"[STV_PROBE] Running tap+ocr age flow at ({x},{y}) n=3 delay=0.5s", flush=True)
        try:
            if not hasattr(agent, "tap_and_capture_age"):
                print("[STV_PROBE] agent.tap_and_capture_age not available", flush=True)
                return 6
            res = agent.tap_and_capture_age(x=x, y=y, n=3, delay=0.5, out_dir="storage/v3")
            print(f"[STV_PROBE] parsed: {res.get('parsed')}", flush=True)
            print(f"[STV_PROBE] images: {res.get('images')}", flush=True)
            print("[STV_PROBE] logs:")
            for L in (res.get("logs") or [])[:200]:
                print(L, flush=True)
        except Exception as e:
            print(f"[STV_PROBE] tap flow error: {type(e).__name__}: {e}", flush=True)
        return 0

    # Ensure stv_refresh uses the same adb path.
    try:
        os.environ.setdefault("ADB_PATH", str(os.getenv("V3_ADB_PATH", "")).strip() or "")
    except Exception:
        pass

    # Ensure Tesseract exists before attempting OCR runs
    tcmd = _find_tesseract_cmd()
    if not tcmd:
        print("[STV_PROBE] ERROR: tesseract not found. Set TESSERACT_CMD or install Tesseract and put it in PATH.", flush=True)
        print("[STV_PROBE] See README for installation instructions.", flush=True)
        return 4
    else:
        if str(os.getenv("V3_ANDROID_DEBUG", "")).strip() == "1":
            print(f"[STV_PROBE] Found tesseract: {tcmd}", flush=True)

    if not url:
        print("[STV_PROBE] Step 1: extract URL via share-sheet (taps on device)", flush=True)
        # run extraction with timeout to avoid hanging if device/input is broken
        ok, res = _run_with_timeout(lambda: agent.copy_current_reel_link_from_share_sheet(), timeout=10.0)
        if not ok:
            print(f"[STV_PROBE] URL extraction failed/timeout: {type(res).__name__}: {res}", flush=True)
            if str(os.getenv("V3_ANDROID_DEBUG", "")).strip() == "1":
                traceback.print_exc()
            return 2
        try:
            url = str(res or "").strip()
        except Exception as e:
            print(f"[STV_PROBE] URL extraction returned invalid result: {type(e).__name__}: {e}", flush=True)
            return 2

    if not url:
        print("[STV_PROBE] URL extraction returned empty. Try --url <reel_url>.", flush=True)
        return 2

    print(f"[STV_PROBE] URL={url}", flush=True)

    print("[STV_PROBE] Step 2: refresh_stv_from_url (open -> screenshot -> OCR -> STV)", flush=True)
    # run full refresh with timeout (allows OCR tries inside refresh)
    ok, res = _run_with_timeout(lambda: refresh_stv_from_url(url, android_agent=agent), timeout=40.0)
    if not ok:
        print(f"[STV_PROBE] refresh_stv_from_url failed/timeout: {type(res).__name__}: {res}", flush=True)
        if str(os.getenv("V3_ANDROID_DEBUG", "")).strip() == "1":
            traceback.print_exc()
        return 3

    # res is expected to be an object with attributes .ok .debug .raw_ocr_text
    try:
        print(f"[STV_PROBE] ok={getattr(res, 'ok', False)}", flush=True)
        print(f"[STV_PROBE] debug={getattr(res, 'debug', None)}", flush=True)
        raw = getattr(res, "raw_ocr_text", None)
        if raw:
            preview = raw.replace("\n", " ")[:180]
            print(f"[STV_PROBE] ocr_preview={preview}", flush=True)
    except Exception as e:
        print(f"[STV_PROBE] Unexpected result shape: {type(e).__name__}: {e}", flush=True)
        return 3

    if getattr(res, "ok", False):
        print("[STV_PROBE] DONE (ok)", flush=True)
        return 0

    print("[STV_PROBE] DONE (failed)", flush=True)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
