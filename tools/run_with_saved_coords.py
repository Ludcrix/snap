import json
import os
import time
import io
import subprocess
from pathlib import Path
import sys

CLICK = Path('storage/v3/stv_click.json')
if not CLICK.exists():
    print('stv_click.json not found — run teach_coords.py first', flush=True)
    raise SystemExit(2)

raw = CLICK.read_text(encoding='utf-8')
jd = json.loads(raw)

# CLI helpers: --auto to accept all, --tesseract <path> to pass tesseract path
RUN_ACCEPT_ALL = bool(os.getenv('RUN_ACCEPT_ALL', '').strip() == '1' or '--auto' in sys.argv)
TESSERACT_ARG = None
if '--tesseract' in sys.argv:
    try:
        i = sys.argv.index('--tesseract')
        if i + 1 < len(sys.argv):
            TESSERACT_ARG = sys.argv[i + 1]
    except Exception:
        TESSERACT_ARG = None

adb = jd.get('adb') or os.getenv('V3_ADB_PATH') or 'adb'
sw = int(jd.get('screen_w') or 0)
sh = int(jd.get('screen_h') or 0)
x_px = int(jd.get('x_px') or 0)
y_px = int(jd.get('y_px') or 0)

# Prefer raw ABS mapping if present
la_x = jd.get('last_abs_x')
la_y = jd.get('last_abs_y')
a_min_x = jd.get('abs_min_x')
a_max_x = jd.get('abs_max_x')
a_min_y = jd.get('abs_min_y')
a_max_y = jd.get('abs_max_y')

def map_abs_to_px(raw_v, amin, amax, span):
    try:
        return int((int(raw_v) - int(amin)) * float(span) / float(int(amax) - int(amin)))
    except Exception:
        return None

if la_x is not None and la_y is not None and a_min_x is not None and a_max_x is not None and a_max_x > a_min_x:
    mx = map_abs_to_px(la_x, a_min_x, a_max_x, sw)
    my = map_abs_to_px(la_y, a_min_y, a_max_y, sh) if a_min_y is not None and a_max_y is not None and a_max_y > a_min_y else None
    if mx is not None:
        x_px = max(0, min(sw, mx))
    if my is not None:
        y_px = max(0, min(sh, my))

print(f'Using adb={adb}', flush=True)
print(f'Screen={sw}x{sh}', flush=True)
print(f'Using tap coords x={x_px} y={y_px}', flush=True)

base = [adb]
logs = []
images = []
ocr_texts = []


def _parse_age_from_text(txt: str) -> str | None:
    """Try to heuristically extract an age from OCR text.

    Returns a human-friendly string like '23 ans' or None.
    """
    if not txt:
        return None
    import re

    # look for patterns like '23 ans' or '23ans' or '23 years'
    m = re.search(r"(\d{1,3})\s*(?:ans|années|annee|years|yrs)\b", txt, re.I)
    if m:
        return f"{int(m.group(1))} ans"

    # look for standalone 2-digit numbers near words like 'âge' or 'age'
    m = re.search(r"(?:âge|age)[:\s]*([1-9][0-9]?)", txt, re.I)
    if m:
        return f"{int(m.group(1))} ans"

    # fallback: any 2-digit number that seems plausible (12-99)
    m = re.search(r"\b([1-9][0-9])\b", txt)
    if m:
        v = int(m.group(1))
        if 12 <= v <= 99:
            return f"{v} ans"

    return None

try:
    cmd_tap = base + ['shell','input','tap', str(int(x_px)), str(int(y_px))]
    logs.append('tap cmd=' + ' '.join(cmd_tap))
    cp = subprocess.run(cmd_tap, capture_output=True, timeout=6.0)
    logs.append(f'tap rc={cp.returncode}')
except Exception as e:
    logs.append(f'tap exc={type(e).__name__}:{e}')

for i in range(3):
    try:
        cmd_sc = base + ['exec-out','screencap','-p']
        logs.append('screencap cmd=' + ' '.join(cmd_sc))
        cp2 = subprocess.run(cmd_sc, capture_output=True, timeout=8.0)
        if int(cp2.returncode) != 0:
            logs.append(f'screencap failed rc={cp2.returncode}')
            continue
        img_bytes = cp2.stdout or b''
        ts = int(time.time())
        fname = os.path.join('storage','v3', f'stv_age_{ts}_{i}.png')
        with open(fname, 'wb') as f:
            f.write(img_bytes)
        images.append(fname)
        logs.append(f'saved {fname}')
        # create an annotated copy with a cross at the tap coords for visual verification
        try:
            try:
                from PIL import Image, ImageDraw
                img = Image.open(io.BytesIO(img_bytes))
            except Exception:
                # fallback to opening saved file
                from PIL import Image, ImageDraw
                img = Image.open(fname)
            draw = ImageDraw.Draw(img)
            # cross size and color
            cross_size = 40
            color = (255, 0, 0)
            x = int(x_px)
            y = int(y_px)
            # draw horizontal and vertical lines
            draw.line((x - cross_size, y, x + cross_size, y), fill=color, width=4)
            draw.line((x, y - cross_size, x, y + cross_size), fill=color, width=4)
            tap_fname = os.path.join('storage','v3', f'stv_age_{ts}_{i}_tap.png')
            img.save(tap_fname)
            logs.append(f'annotation saved {tap_fname}')
            images.append(tap_fname)
        except Exception as e:
            logs.append(f'annotation_failed:{type(e).__name__}:{e}')
        # interactive validation before OCR: open annotated image and ask user
        accept_all = globals().get('_RUN_ACCEPT_ALL', False)
        try:
            tap_img = tap_fname if 'tap_fname' in locals() else None
            if tap_img and os.path.exists(tap_img):
                # open image for user review (Windows: os.startfile)
                try:
                    if os.name == 'nt':
                        os.startfile(tap_img)
                    else:
                        # try common openers on non-windows
                        for cmd in ("xdg-open", "open"):
                            try:
                                subprocess.Popen([cmd, tap_img])
                                break
                            except Exception:
                                continue
                except Exception:
                    pass

            do_ocr = False
            if accept_all:
                do_ocr = True
            else:
                # prompt user
                try:
                    resp = input(f"Validate image {tap_img} for OCR? (y=ocr, n=skip, a=accept all, q=quit) ").strip().lower()
                except Exception:
                    resp = 'y'
                if resp == 'a':
                    globals()['_RUN_ACCEPT_ALL'] = True
                    do_ocr = True
                elif resp == 'y':
                    do_ocr = True
                elif resp == 'q':
                    # stop further processing
                    break
                else:
                    do_ocr = False

            if do_ocr:
                try:
                    from PIL import Image
                    import pytesseract, shutil
                except Exception:
                    logs.append('ocr unavailable (Pillow/pytesseract missing)')
                    time.sleep(0.5)
                    continue

                tcmd = (TESSERACT_ARG or '').strip() or str(os.getenv('TESSERACT_CMD','')).strip() or str(shutil.which('tesseract') or '').strip()
                if not tcmd and os.name == 'nt':
                    candidates = [
                        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                    ]
                    for p in candidates:
                        if os.path.exists(p):
                            tcmd = p
                            break
                if not tcmd and os.name == 'nt':
                    roots = [os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)')]
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
                                tcmd = os.path.join(dirpath, 'tesseract.exe')
                                break
                            if depth >= max_depth:
                                dirnames[:] = []
                        if tcmd:
                            break
                if tcmd:
                    try:
                        pytesseract.pytesseract.tesseract_cmd = tcmd
                    except Exception:
                        pass

                try:
                    img = Image.open(io.BytesIO(img_bytes))
                    cfg = '--psm 6'
                    try:
                        text = str(pytesseract.image_to_string(img, lang='fra+eng', config=cfg) or '')
                    except Exception:
                        text = str(pytesseract.image_to_string(img, config=cfg) or '')
                    ocr_texts.append(text)
                    logs.append(f'ocr #{i} len={len(text)}')
                except Exception as e:
                    logs.append(f'ocr_failed:{type(e).__name__}:{e}')
        except Exception as e:
            logs.append(f'ocr_prompt_exc:{type(e).__name__}:{e}')
        time.sleep(0.5)
    except Exception as e:
        logs.append(f'capture_exc #{i}: {type(e).__name__}:{e}')

print('IMAGES:', images, flush=True)
print('LOGS:')
for L in logs:
    print(L, flush=True)

# Try to parse an age from OCR results (prefer first non-None)
parsed_age = None
for t in (ocr_texts or []):
    if not t:
        continue
    parsed_age = _parse_age_from_text(t)
    if parsed_age:
        break

print('PARSED_AGE:', parsed_age, flush=True)

for i,t in enumerate(ocr_texts or []):
    print(f'--- OCR {i} len={len(t or "")} ---', flush=True)
    print((t or '')[:1000], flush=True)
