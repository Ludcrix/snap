import argparse
import json
import os
import time
import io
import threading
import queue
import subprocess
from bot.v3.android_agent import AndroidAgent, AndroidAgentConfig
from pathlib import Path

CLICK = Path("storage/v3/stv_click.json")
if not CLICK.exists():
    print("stv_click.json not found", flush=True)
    raise SystemExit(2)

raw = CLICK.read_text(encoding="utf-8")
try:
    jd = json.loads(raw)
except Exception as e:
    print("failed parse stv_click.json", e, flush=True)
    raise SystemExit(3)

adb = jd.get("adb") or os.getenv("V3_ADB_PATH") or "adb"
sw = int(jd.get("screen_w") or 0)
sh = int(jd.get("screen_h") or 0)
x_px = int(jd.get("x_px") or 0)
y_px = int(jd.get("y_px") or 0)
x_ratio = jd.get("x_ratio")
y_ratio = jd.get("y_ratio")

# Normalize adb path
adb = str(adb or "").strip() or "adb"

# Use stored coordinates from JSON strictly when available.
# If stored pixel values are invalid (0 or non-numeric), fall back to ratios or sensible defaults.
try:
    x = int(x_px)
    if x <= 0:
        raise ValueError()
except Exception:
    if isinstance(x_ratio, (int, float)) and sw > 0:
        x = int(float(x_ratio) * sw)
    else:
        x = 100

try:
    y = int(y_px)
    if y <= 0:
        raise ValueError()
except Exception:
    if isinstance(y_ratio, (int, float)) and sh > 0:
        y = int(float(y_ratio) * sh)
    else:
        y = 240

print(f"Using adb={adb}", flush=True)
print(f"Screen={sw}x{sh}", flush=True)
print(f"Using tap coords x={x} y={y}", flush=True)


cfg = AndroidAgentConfig(adb_path=adb, allow_input=True)
agent = AndroidAgent(cfg)

# Mode: click the start of the video title instead of teach point
CLICK_TITLE = str(os.getenv('V3_CLICK_TITLE','')).strip() == '1'

def find_title_coords_via_ocr(adb_cmd_base, sw, sh):
    """Capture a screencap and try to find a topmost text bounding box via pytesseract.
    Returns (x,y,method) or (None,None,reason).
    """
    try:
        import tempfile, shutil
        from PIL import Image
        import pytesseract
    except Exception as e:
        return None, None, f'pytesseract_missing:{e}'

    # ensure pytesseract knows where tesseract binary is (env override or PATH)
    try:
        tcmd = str(os.getenv('TESSERACT_CMD','')).strip()
        if not tcmd:
            tcmd = str(shutil.which('tesseract') or '').strip()
        if tcmd:
            try:
                pytesseract.pytesseract.tesseract_cmd = tcmd
            except Exception:
                pass
    except Exception:
        pass

    try:
        # capture full screencap then crop to lower third where the title is expected
        cp = subprocess.run(adb_cmd_base + ['exec-out','screencap','-p'], stdout=subprocess.PIPE, timeout=8)
        data = cp.stdout
        if not data:
            return None, None, 'screencap_failed'
        # load image
        img = Image.open(io.BytesIO(data)).convert('RGB')
        # crop to lower third: titre attendu entre la photo de profil et la boîte "ajoutez un commentaire"
        top_crop = int(sh * 2 / 3) if sh else int(img.size[1] * 2 / 3)
        crop = img.crop((0, top_crop, img.size[0], img.size[1]))
        # save crop for debugging (explicit, single named file)
        debug_path = os.path.join('storage','v3','title_crop.png')
        try:
            os.makedirs(os.path.dirname(debug_path), exist_ok=True)
            crop.save(debug_path)
            print('LOWER_CROP:', debug_path, flush=True)
        except Exception as e:
            print('LOWER_CROP: save_failed:', e, flush=True)
        # read optional preferences for title zone boosting
        try:
            crop_w, crop_h = crop.size[0], crop.size[1]
        except Exception:
            crop_w = crop_h = None
        try:
            V3_TITLE_TOP_MIN = float(os.getenv('V3_TITLE_TOP_MIN','0.18'))
        except Exception:
            V3_TITLE_TOP_MIN = 0.18
        try:
            V3_TITLE_TOP_MAX = float(os.getenv('V3_TITLE_TOP_MAX','0.55'))
        except Exception:
            V3_TITLE_TOP_MAX = 0.55
        try:
            V3_TITLE_MIN_WIDTH_RATIO = float(os.getenv('V3_TITLE_MIN_WIDTH_RATIO','0.30'))
        except Exception:
            V3_TITLE_MIN_WIDTH_RATIO = 0.30
        # use image_to_data on the crop to get boxes (operate only on lower-third)
        try:
            data = pytesseract.image_to_data(crop, output_type=pytesseract.Output.DICT, lang='fra+eng')
        except Exception:
            data = pytesseract.image_to_data(crop, output_type=pytesseract.Output.DICT)
        n = len(data.get('text', []))
        candidates = []
        for i in range(n):
            txt = (data.get('text') or [])[i]
            if not txt or not txt.strip():
                continue
            left = int(data.get('left')[i])
            top = int(data.get('top')[i])
            width = int(data.get('width')[i])
            height = int(data.get('height')[i])
            candidates.append({'text': txt.strip(), 'left': left, 'top': top, 'w': width, 'h': height})

        if not candidates:
            return None, None, 'no_text_found'

        # Group nearby word boxes into line-level candidates (words on same horizontal band)
        try:
            lines_map = {}
            for c in candidates:
                # bucket by top coordinate (30px tall bands)
                key = int(c['top'] // 30)
                lines_map.setdefault(key, []).append(c)
            line_candidates = []
            for grp in lines_map.values():
                grp_sorted = sorted(grp, key=lambda x: x['left'])
                combined = ' '.join(x['text'] for x in grp_sorted).strip()
                lefts = [x['left'] for x in grp_sorted]
                tops = [x['top'] for x in grp_sorted]
                rights = [x['left'] + x['w'] for x in grp_sorted]
                heights = [x['h'] for x in grp_sorted]
                l = min(lefts)
                t = min(tops)
                r = max(rights)
                h = max(heights)
                w = r - l
                line_candidates.append({'text': combined, 'left': l, 'top': t, 'w': w, 'h': h})
            # replace candidates with merged line candidates
            if line_candidates:
                candidates = line_candidates
        except Exception:
            pass

        # Draw boxes around every detected candidate on the crop and save an annotated PNG
        try:
            try:
                from PIL import ImageDraw, ImageFont
            except Exception:
                ImageDraw = None
            if ImageDraw is not None:
                try:
                    ann = crop.convert('RGBA')
                    draw = ImageDraw.Draw(ann)
                    # try to load a reasonable font, fallback if not available
                    try:
                        font = ImageFont.truetype('arial.ttf', 14)
                    except Exception:
                        try:
                            font = ImageFont.load_default()
                        except Exception:
                            font = None
                    for ai, cc in enumerate(candidates):
                        try:
                            l = int(cc.get('left', 0))
                            t = int(cc.get('top', 0))
                            w = int(cc.get('w', 0))
                            h = int(cc.get('h', 0))
                            # draw rectangle
                            draw.rectangle([l, t, l + w, t + h], outline=(255, 0, 0, 255), width=3)
                            # draw label background
                            lab = str(ai)
                            tw = 20
                            th = 16
                            bx0 = l
                            by0 = max(0, t - th - 2)
                            bx1 = l + tw
                            by1 = by0 + th
                            draw.rectangle([bx0, by0, bx1, by1], fill=(255, 0, 0, 200))
                            # draw index text
                            if font is not None:
                                draw.text((bx0 + 3, by0 + 1), lab, fill=(255, 255, 255, 255), font=font)
                            else:
                                draw.text((bx0 + 3, by0 + 1), lab, fill=(255, 255, 255, 255))
                        except Exception:
                            pass
                    ann_path = os.path.join('storage', 'v3', f'title_crop_boxes.png')
                    try:
                        ann.save(ann_path)
                        print('LOWER_CROP_BOXES:', ann_path, flush=True)
                    except Exception as e:
                        print('LOWER_CROP_BOXES: save failed', e, flush=True)
                except Exception as e:
                    print('LOWER_CROP_BOXES: draw failed', e, flush=True)
        except Exception:
            pass

        # get full OCR text for the crop (human-friendly debug)
        try:
            try:
                full_text = pytesseract.image_to_string(crop, lang='fra+eng')
            except Exception:
                full_text = pytesseract.image_to_string(crop)
            print('OCR_FULL_TEXT:\n' + (full_text or '').strip(), flush=True)
        except Exception:
            print('OCR_FULL_TEXT: unavailable', flush=True)

        # compute score for candidates and log them clearly
        import re as _re
        scored = []
        for idx, c in enumerate(candidates):
            raw_txt = str(c.get('text') or '').replace('\n', ' ').strip()
            txt_l = raw_txt.lower()
            words = [w for w in _re.split(r"\s+", txt_l) if w]
            wc = len(words)
            # base score: prefer longer text and multiple words
            score = len(raw_txt) * 12 + wc * 120

            # smaller penalty for vertical position (prefer lines higher in crop)
            score -= int(c['top'] * 0.5)

            # strong boost if line starts near the left (under avatar)
            try:
                crop_w = crop.size[0]
                if c['left'] < int(crop_w * 0.45):
                    score += 200
            except Exception:
                pass

            # penalize handles/usernames and UI labels
            if any(w.startswith('@') for w in words):
                score -= 300
            if any(k in txt_l for k in ('ajoutez', 'commentaire', 'commentaire...')):
                score -= 1000

            # penalize engagement counters like '154K', '6,4M', standalone numbers
            if _re.search(r"^\d+[\.,]?\d*[kmKM]?$", txt_l) or any(_re.match(r"^\d+[\.,]?\d*[kmKM]?$", w) for w in words):
                score -= 600

            # penalize very short lines
            if len(raw_txt) < 3 or wc == 0:
                score -= 400

            # boost candidates that sit in the user-prioritized vertical band and are wide enough
            try:
                if crop_h and crop_w:
                    top_frac = float(c['top']) / float(crop_h)
                    width_frac = float(c['w']) / float(crop_w)
                    if top_frac >= V3_TITLE_TOP_MIN and top_frac <= V3_TITLE_TOP_MAX and width_frac >= V3_TITLE_MIN_WIDTH_RATIO:
                        score += 400
                        # mark with special flag by appending additional info in tuple
            except Exception:
                pass

            scored.append((score, c, raw_txt, wc))

        if not scored:
            print('OCR_CANDIDATES: none found', flush=True)
        else:
            print('OCR_CANDIDATES: (idx score wc left top w h) text', flush=True)
            for i, (score, c, raw_txt, wc) in enumerate(scored):
                txt = raw_txt
                if len(txt) > 120:
                    txt = txt[:117] + '...'
                print(f" - {i:02d} {score:6d} {wc:2d} {c['left']:5d} {c['top']:5d} {c['w']:4d} {c['h']:4d} '{txt}'", flush=True)

        # choose best candidate: allow env override `V3_CLICK_INDEX`, click bbox center
        click_index = None
        try:
            raw = os.getenv('V3_CLICK_INDEX','').strip()
            click_index = int(raw) if raw != '' else None
        except Exception:
            click_index = None

        # pick candidate by index or by top score
        chosen = None
        if click_index is not None:
            if 0 <= click_index < len(scored):
                chosen = scored[click_index]
                print(f"NOTE: V3_CLICK_INDEX={click_index} forcing candidate selection", flush=True)
            else:
                print(f"NOTE: V3_CLICK_INDEX={click_index} out of range, using top candidate", flush=True)
        if chosen is None:
            # attempt to prefer left-aligned multi-word candidates with alphabetic chars
            try:
                crop_w = crop.size[0]
            except Exception:
                crop_w = None
            filtered = []
            for item in scored:
                score_i, c_i, raw_txt_i, wc_i = item
                has_alpha = bool(_re.search(r'[A-Za-zÀ-ÖØ-öø-ÿ]', raw_txt_i))
                left_ok = (crop_w is None) or (c_i['left'] < int((crop_w or 0) * 0.45))
                if wc_i >= 2 and has_alpha and left_ok:
                    filtered.append(item)
            if filtered:
                # if there are multiple left-aligned multi-word candidates, prefer the one nearest the top
                try:
                    left_aligned = [f for f in filtered if f[3] >= 2]
                    if left_aligned:
                        chosen = min(left_aligned, key=lambda s: s[1]['top'])
                    else:
                        chosen = max(filtered, key=lambda s: s[0])
                except Exception:
                    chosen = max(filtered, key=lambda s: s[0])
            else:
                chosen = max(scored, key=lambda s: s[0])
            # strict filtering: prefer candidates that contain alphabetic characters and at least one word >2 chars
            try:
                strong = []
                for item in scored:
                    score_i, c_i, raw_txt_i, wc_i = item
                    words_i = [w for w in _re.split(r"\s+", raw_txt_i) if w]
                    has_alpha = bool(_re.search(r'[A-Za-zÀ-ÖØ-öø-ÿ]', raw_txt_i))
                    if not has_alpha:
                        continue
                    if any(len(w) > 2 for w in words_i):
                        strong.append(item)
                if strong:
                    # among strong, prefer left-aligned ones
                    left_strong = []
                    for s in strong:
                        if crop_w and s[1]['left'] < int(crop_w * 0.45):
                            left_strong.append(s)
                    if left_strong:
                        chosen = min(left_strong, key=lambda s: s[1]['top'])
                    else:
                        chosen = max(strong, key=lambda s: s[0])
            except Exception:
                pass

        # unpack chosen which may include extra metadata
        if isinstance(chosen, (list, tuple)):
            score = chosen[0]
            c = chosen[1]
        else:
            score = chosen
            c = None
        # compute click near the start (left) of the title line so the tap lands on the title
        cx = c['left'] + max(6, min(24, c['w'] // 4))
        cy = c['top'] + max(1, c['h'] // 2)
        cy = cy + top_crop
        txt = c['text'].replace('\n', ' ').strip()
        print(f"CHOSE_CANDIDATE: idx_text='{txt}' score={score} bbox=({c['left']},{c['top']},{c['w']},{c['h']}) -> click_center ({cx},{cy})", flush=True)
        return cx, cy, 'ocr_crop_lower_third'
    except Exception as e:
        return None, None, f'ocr_exc:{e}'



def detect_and_fix_swapped(x_val, y_val, sw_val, sh_val):
    """Heuristic: detect if coordinates look swapped and fix them.
    Returns (x,y,swapped)
    """
    try:
        xi = int(x_val)
        yi = int(y_val)
    except Exception:
        return x_val, y_val, False

    # If both already within bounds, assume OK
    if sw_val > 0 and sh_val > 0 and 0 <= xi <= sw_val and 0 <= yi <= sh_val:
        return xi, yi, False

    # If x is out of width but y fits width/height, it's likely swapped
    if sw_val > 0 and sh_val > 0:
        if xi > sw_val and 0 <= yi <= sw_val and 0 <= yi <= sh_val:
            return yi, xi, True
        if yi > sh_val and 0 <= xi <= sh_val and 0 <= xi <= sw_val:
            return yi, xi, True

    return xi, yi, False


def try_parse_num(tok: str):
    tok = str(tok).strip()
    if not tok:
        raise ValueError("empty")
    # try int with base auto
    try:
        return int(tok, 0)
    except Exception:
        pass
    # try hex without 0x
    try:
        return int(tok, 16)
    except Exception:
        pass
    # fallback: extract digits
    import re
    m = re.search(r"[0-9a-fA-F]+", tok)
    if m:
        return int(m.group(0), 16 if any(c in 'abcdefABCDEF' for c in m.group(0)) else 10)
    raise ValueError(f"cannot parse num: {tok}")


def listen_getevent_and_parse(adb_cmd_base, timeout=10.0):
    """Run `adb shell getevent -lt` for up to `timeout` seconds.

    Returns: (lines, last_x, last_y, (min_x, max_x, min_y, max_y), error_or_none)
    """
    lines = []
    last_x = None
    last_y = None
    min_x = max_x = min_y = max_y = None

    try:
        proc = subprocess.Popen(adb_cmd_base + ["shell", "getevent", "-lt"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except Exception as e:
        return lines, None, None, (None, None, None, None), f"start_getevent_exc:{e}"

    q = queue.Queue()

    def reader_thread():
        try:
            for ln in proc.stdout:
                q.put(ln)
        except Exception:
            pass

    t = threading.Thread(target=reader_thread, daemon=True)
    t.start()

    end = time.time() + float(timeout)
    import re
    try:
        while time.time() < end:
            try:
                ln = q.get(timeout=0.25)
            except queue.Empty:
                continue
            s = ln.rstrip('\n')
            lines.append(s)
            l = s.strip()

            # Prefer explicit ABS_X / ABS_Y lines
            if 'ABS_X' in l or 'ABS_Y' in l:
                parts = l.split()
                if parts:
                    tok = parts[-1]
                    try:
                        v = try_parse_num(tok)
                    except Exception:
                        v = None
                    if 'ABS_X' in l:
                        if v is not None and v != 0:
                            last_x = v
                        if v is not None:
                            if min_x is None or v < min_x:
                                min_x = v
                            if max_x is None or v > max_x:
                                max_x = v
                    if 'ABS_Y' in l:
                        if v is not None and v != 0:
                            last_y = v
                        if v is not None:
                            if min_y is None or v < min_y:
                                min_y = v
                            if max_y is None or v > max_y:
                                max_y = v
                continue

            # Fallback: find hex/number tokens and take last two as x,y
            toks = re.findall(r"\b[0-9a-fA-F]{2,}\b", l)
            if toks:
                try:
                    if len(toks) >= 2:
                        tx = try_parse_num(toks[-2])
                        ty = try_parse_num(toks[-1])
                        if tx is not None and tx != 0:
                            last_x = tx
                        if ty is not None and ty != 0:
                            last_y = ty
                        if tx is not None:
                            if min_x is None or tx < min_x:
                                min_x = tx
                            if max_x is None or tx > max_x:
                                max_x = tx
                        if ty is not None:
                            if min_y is None or ty < min_y:
                                min_y = ty
                            if max_y is None or ty > max_y:
                                max_y = ty
                    else:
                        v = try_parse_num(toks[0])
                        if v is not None and v != 0:
                            if last_x is None:
                                last_x = v
                            if v is not None:
                                if min_x is None or v < min_x:
                                    min_x = v
                                if max_x is None or v > max_x:
                                    max_x = v
                        else:
                            if v is not None and v != 0:
                                last_y = v
                                if min_y is None or v < min_y:
                                    min_y = v
                                if max_y is None or v > max_y:
                                    max_y = v
                except Exception:
                    pass

    except Exception:
        pass
    finally:
        try:
            proc.terminate()
        except Exception:
            pass

    return lines, last_x, last_y, (min_x, max_x, min_y, max_y), None
    

    return lines, last_x, last_y, (min_x, max_x, min_y, max_y), None


"""Teach step or skip behaviour.

If environment variable V3_SKIP_TEACH=1 is set, skip listening and use
the saved coordinates from `storage/v3/stv_click.json`.
"""
# Skip teach when explicitly requested; allow forcing teach via V3_FORCE_TEACH=1
skip_teach = str(os.getenv('V3_SKIP_TEACH','')).strip() == '1'
force_teach = str(os.getenv('V3_FORCE_TEACH','')).strip() == '1'
if force_teach:
    skip_teach = False
    print('\nFORCE_TEACH enabled via V3_FORCE_TEACH=1: running teach step', flush=True)

# If CLICK_TITLE mode is set, bypass teach listening and compute click coords immediately
if CLICK_TITLE:
    print('\nCLICK_TITLE immediate mode: will attempt to locate title and click without teach/listen', flush=True)
    try:
        base = agent._adb_base()
        tx, ty, method = find_title_coords_via_ocr(base, sw, sh)
        if tx is not None and ty is not None:
            print(f'CLICK_TITLE: located via {method} -> x={tx} y={ty}', flush=True)
            x = tx
            y = ty
        else:
            print(f'CLICK_TITLE: locate failed ({method}), using heuristic', flush=True)
            x = max(20, int(sw * 0.08)) if sw else 100
            y = max(20, int(sh * 0.12)) if sh else 200
            print(f'CLICK_TITLE heuristic coords -> x={x} y={y}', flush=True)
        use_coords = True
        # ensure we skip the teach branch
        skip_teach = True
    except Exception as e:
        print('CLICK_TITLE immediate mode failed:', type(e).__name__, e, flush=True)
        # fall through to normal flow
if skip_teach:
    print('\n-- Skipping teach: using saved coordinates from stv_click.json --', flush=True)
    try:
        # raw values from JSON
        la_x = jd.get('last_abs_x')
        la_y = jd.get('last_abs_y')
        a_min_x = jd.get('abs_min_x')
        a_max_x = jd.get('abs_max_x')
        a_min_y = jd.get('abs_min_y')
        a_max_y = jd.get('abs_max_y')

        print("SOURCE (JSON): x_px,y_px =", x_px, y_px,
              " last_abs_x,last_abs_y =", la_x, la_y,
              " abs_min_x,abs_max_x =", a_min_x, a_max_x,
              " abs_min_y,abs_max_y =", a_min_y, a_max_y, flush=True)

        def to_int(v):
            try:
                return int(v)
            except Exception:
                return None

        # prefer explicit pixel coords when valid
        px_x = x_px if isinstance(x_px, int) and x_px > 0 else None
        px_y = y_px if isinstance(y_px, int) and y_px > 0 else None

        la_x_i = to_int(la_x)
        la_y_i = to_int(la_y)
        a_min_x_i = to_int(a_min_x)
        a_max_x_i = to_int(a_max_x)
        a_min_y_i = to_int(a_min_y)
        a_max_y_i = to_int(a_max_y)

        mapped_x = None
        mapped_y = None

        # Only map ABS->px if explicit px not present
        if px_x is None and la_x_i is not None and a_min_x_i is not None and a_max_x_i is not None and sw > 0:
            span_x = a_max_x_i - a_min_x_i
            if span_x > 0:
                rel_x = (la_x_i - a_min_x_i) / span_x
                mapped_x = int(rel_x * sw)
                print(f"DEBUG: mapped_x raw={mapped_x} rel={rel_x:.6f}", flush=True)

        if px_y is None and la_y_i is not None and a_min_y_i is not None and a_max_y_i is not None and sh > 0:
            span_y = a_max_y_i - a_min_y_i
            if span_y > 0:
                rel_y = (la_y_i - a_min_y_i) / span_y
                mapped_y = int(rel_y * sh)
                print(f"DEBUG: mapped_y raw={mapped_y} rel={rel_y:.6f}", flush=True)

        # clamp to valid pixel indices (0 .. size-1)
        if mapped_x is not None and sw > 0:
            mapped_x = max(0, min(sw - 1, mapped_x))
        if mapped_y is not None and sh > 0:
            mapped_y = max(0, min(sh - 1, mapped_y))

        # choose final x,y: explicit pixel coords preferred, then mapped, then existing fallback x/y
        if px_x is not None:
            x = px_x
            print("DEBUG: using explicit x_px from JSON", x, flush=True)
        elif mapped_x is not None:
            x = mapped_x
            print("DEBUG: using mapped_x", x, flush=True)

        if px_y is not None:
            y = px_y
            print("DEBUG: using explicit y_px from JSON", y, flush=True)
        elif mapped_y is not None:
            y = mapped_y
            print("DEBUG: using mapped_y", y, flush=True)

        # persist the resolved pixel coords back into the JSON so subsequent runs are stable
        try:
            jd['x_px'] = int(x)
            jd['y_px'] = int(y)
            CLICK.write_text(json.dumps(jd, ensure_ascii=False, indent=2), encoding='utf-8')
            print("DEBUG: updated stv_click.json with x_px,y_px", x, y, flush=True)
        except Exception as e:
            print("DEBUG: failed to write updated x_px/y_px:", e, flush=True)

        print("FINAL_COORD_SOURCE: using x,y =", x, y, " (mapped if available, else fallback)", flush=True)
        use_coords = True
    except Exception as e:
        print("DEBUG: exception in skip_teach mapping:", e, flush=True)
        use_coords = True
else:
    # Teach step
    print('\n-- Teach step: vous avez 10s pour taper avec le stylet sur la tablette --', flush=True)
    print('Listening for getevent (10s)...', flush=True)
    lines, gx, gy, ranges, gerr = listen_getevent_and_parse([adb])
    min_x = max_x = min_y = max_y = None
    if ranges:
        try:
            min_x, max_x, min_y, max_y = ranges
        except Exception:
            min_x = max_x = min_y = max_y = None
    ts = int(time.time())
    logp = os.path.join('storage','v3', f'stv_getevent_{ts}.log')
    try:
        os.makedirs(os.path.dirname(logp), exist_ok=True)
        with open(logp, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
    except Exception:
        pass
    print('getevent log saved to', logp, flush=True)
    if gerr:
        print('getevent error:', gerr, flush=True)
    print('Detected coords (raw):', gx, gy, 'ranges:', min_x, max_x, min_y, max_y, flush=True)
    use_coords = False
    if gx is not None and gy is not None:
        # Single-point teach: derive exact pixel coords from the single ABS point
        def to_int(v):
            try:
                return int(v)
            except Exception:
                return None

        gx_i = to_int(gx)
        gy_i = to_int(gy)

        # validate ranges; if invalid, fallback to sensible full-range (0..32767)
        def valid_range(amin, amax):
            try:
                amin_i = int(amin)
                amax_i = int(amax)
            except Exception:
                return False
            if amin_i in (0, 4294967295) or amax_i in (0, 4294967295):
                return False
            span = amax_i - amin_i
            if span <= 0 or span < 100:
                return False
            return True

        if valid_range(min_x, max_x):
            a_min_x_s, a_max_x_s = int(min_x), int(max_x)
        else:
            a_min_x_s, a_max_x_s = 0, 32767
            print('DEBUG: X range invalid or missing; falling back to 0..32767', flush=True)
        if valid_range(min_y, max_y):
            a_min_y_s, a_max_y_s = int(min_y), int(max_y)
        else:
            a_min_y_s, a_max_y_s = 0, 32767
            print('DEBUG: Y range invalid or missing; falling back to 0..32767', flush=True)

        derived_x = None
        derived_y = None
        if gx_i is not None and sw > 0:
            span_x = max(1, a_max_x_s - a_min_x_s)
            rel_x = (gx_i - a_min_x_s) / float(span_x)
            derived_x = int(rel_x * sw)
            derived_x = max(0, min(sw - 1, derived_x))
        if gy_i is not None and sh > 0:
            span_y = max(1, a_max_y_s - a_min_y_s)
            rel_y = (gy_i - a_min_y_s) / float(span_y)
            derived_y = int(rel_y * sh)
            derived_y = max(0, min(sh - 1, derived_y))

        print('Teach -> derived pixel coords from single ABS point:', derived_x, derived_y, flush=True)

        # final selection: prefer any existing explicit x_px/y_px in JSON if valid, else use derived
        try:
            cur = {}
            if CLICK.exists():
                cur = json.loads(CLICK.read_text(encoding='utf-8') or '{}')
        except Exception:
            cur = {}

        px_x = to_int(cur.get('x_px')) if cur.get('x_px') is not None else None
        px_y = to_int(cur.get('y_px')) if cur.get('y_px') is not None else None

        if px_x is not None and px_x > 0:
            x = px_x
            print('Using explicit x_px from JSON:', x, flush=True)
        elif derived_x is not None:
            x = derived_x
            print('Using derived x from ABS:', x, flush=True)

        if px_y is not None and px_y > 0:
            y = px_y
            print('Using explicit y_px from JSON:', y, flush=True)
        elif derived_y is not None:
            y = derived_y
            print('Using derived y from ABS:', y, flush=True)

        # persist resolved pixel coordinates into storage for stability
        try:
            if not cur:
                cur = {}
            cur.update({'adb': adb, 'x_px': int(x), 'y_px': int(y)})
            if sw>0 and sh>0:
                cur['screen_w'] = sw
                cur['screen_h'] = sh
                cur['x_ratio'] = float(x)/float(sw)
                cur['y_ratio'] = float(y)/float(sh)
            CLICK.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding='utf-8')
            print('Saved explicit coords to', CLICK, flush=True)
        except Exception as e:
            print('Failed to persist coords:', e, flush=True)

        use_coords = True
    else:
        print('No coords detected in getevent output.', flush=True)


print("Tapping once then capturing 3 screenshots (0.5s pause)", flush=True)
base = agent._adb_base()
logs = []
images = []
ocr_texts = []
parsed = None
try:
    # Option: click at start of video title (try OCR, fallback to heuristic)
    if CLICK_TITLE:
        print('CLICK_TITLE mode enabled: attempting to locate video title start', flush=True)
        tx, ty, method = find_title_coords_via_ocr(base, sw, sh)
        if tx is not None and ty is not None:
            print(f'CLICK_TITLE: found coords via {method} -> x={tx} y={ty}', flush=True)
            x = tx
            y = ty
        else:
            print(f'CLICK_TITLE: failed to locate title via OCR ({method}), using heuristic', flush=True)
            # heuristic: left margin, about 15% from top
            try:
                hx = max(20, int(sw * 0.08)) if sw else 100
                hy = max(20, int(sh * 0.12)) if sh else 200
                print(f'CLICK_TITLE heuristic coords -> x={hx} y={hy}', flush=True)
                x = hx
                y = hy
            except Exception:
                x = x
                y = y

    # single tap
    cmd_tap = base + ["shell", "input", "tap", str(int(x)), str(int(y))]
    logs.append(f"tap cmd={' '.join(cmd_tap)}")
    try:
        import subprocess
        cp = subprocess.run(cmd_tap, capture_output=True, timeout=5.0)
        logs.append(f"tap rc={cp.returncode}")
    except Exception as e:
        logs.append(f"tap exc={type(e).__name__}:{e}")

    # captures
    if CLICK_TITLE:
        try:
            cmd_sc = base + ["exec-out", "screencap", "-p"]
            logs.append(f"screencap cmd={' '.join(cmd_sc)}")
            cp2 = subprocess.run(cmd_sc, capture_output=True, timeout=8.0)
            if int(cp2.returncode) != 0:
                logs.append(f"screencap failed rc={cp2.returncode}")
            else:
                img_bytes = cp2.stdout or b""
                try:
                    from PIL import Image
                    img = Image.open(io.BytesIO(img_bytes)).convert('RGB')
                    top_crop = int(sh * 2 / 3) if sh else int(img.size[1] * 2 / 3)
                    crop = img.crop((0, top_crop, img.size[0], img.size[1]))
                    ts = int(time.time())
                    fname = os.path.join('storage', 'v3', f"stv_age_titlecrop_{ts}.png")
                    os.makedirs(os.path.dirname(fname), exist_ok=True)
                    crop.save(fname)
                    images.append(fname)
                    logs.append(f"saved lower-third crop: {fname}")

                    # run OCR on the crop only and record text
                    try:
                        import pytesseract, shutil
                        tcmd = str(os.getenv('TESSERACT_CMD','')).strip()
                        if not tcmd:
                            tcmd = str(shutil.which('tesseract') or '').strip()
                        if tcmd:
                            try:
                                pytesseract.pytesseract.tesseract_cmd = tcmd
                            except Exception:
                                pass
                        cfg = '--psm 6'
                        try:
                            text = str(pytesseract.image_to_string(crop, lang='fra+eng', config=cfg) or '')
                        except Exception:
                            text = str(pytesseract.image_to_string(crop, config=cfg) or '')
                        ocr_texts.append(text)
                        logs.append(f"ocr crop len={len(text)}")
                        print('DEBUG: OCR full text from lower-third crop:\n', text, flush=True)
                    except Exception:
                        logs.append('ocr unavailable (Pillow/pytesseract missing or tesseract not installed)')

                except Exception as e:
                    logs.append(f"save_crop_exc: {e}")
        except Exception as e:
            logs.append(f"capture_exc: {type(e).__name__}:{e}")
    else:
        for i in range(3):
            try:
                cmd_sc = base + ["exec-out", "screencap", "-p"]
                logs.append(f"screencap #{i} cmd={' '.join(cmd_sc)}")
                cp2 = subprocess.run(cmd_sc, capture_output=True, timeout=8.0)
                if int(cp2.returncode) != 0:
                    logs.append(f"screencap #{i} failed rc={cp2.returncode}")
                    continue
                img_bytes = cp2.stdout or b""
                ts = int(time.time())
                fname = os.path.join('storage', 'v3', f"stv_age_{ts}_{i}.png")
                try:
                    with open(fname, 'wb') as f:
                        f.write(img_bytes)
                    images.append(fname)
                    logs.append(f"saved screenshot #{i}: {fname}")
                except Exception as e:
                    logs.append(f"save_screenshot_exc #{i}: {e}")

                # OCR attempt
                print(f"Before OCR: processing image {fname} with tap coords x={x} y={y}", flush=True)
                try:
                    from PIL import Image
                    import pytesseract
                    import shutil
                    tcmd = str(os.getenv('TESSERACT_CMD','')).strip()
                    if not tcmd:
                        tcmd = str(shutil.which('tesseract') or '').strip()
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
                        logs.append(f"ocr #{i} len={len(text)}")
                    except Exception as e:
                        logs.append(f"ocr_image_open_exc #{i}: {e}")
                except Exception:
                    logs.append('ocr unavailable (Pillow/pytesseract missing)')

                # parse quick patterns
                try:
                    import re
                    for text in (ocr_texts or []):
                        m = re.search(r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})", text)
                        if m:
                            parsed = m.group(1)
                            break
                        m2 = re.search(r"(\d{1,3})\s*(ans|years)", text, re.IGNORECASE)
                        if m2:
                            parsed = f"{m2.group(1)} ans"
                            break
                except Exception as e:
                    logs.append(f"parse_exc: {e}")

                try:
                    time.sleep(0.5)
                except Exception:
                    pass

            except Exception as e:
                logs.append(f"iteration_exc #{i}: {type(e).__name__}:{e}")
except Exception as e:
    logs.append(f"unexpected test exc: {type(e).__name__}:{e}")

print("PARSED:", parsed, flush=True)
print("IMAGES:", images, flush=True)
print("LOGS:")
for L in logs:
    print(L, flush=True)
for i,t in enumerate(ocr_texts or []):
    print(f"--- OCR {i} len={len(t)} ---", flush=True)
    print((t or '')[:1000], flush=True)

# Annotate one of the saved screenshots with a visible cross at the tap location
try:
    if images:
        try:
            from PIL import Image, ImageDraw
            img_path = images[len(images)//2]
            img = Image.open(img_path).convert('RGBA')
            iw, ih = img.size
            # map device coords -> image coords
            if sw > 0 and sh > 0:
                scale_x = float(iw) / float(sw)
                scale_y = float(ih) / float(sh)
            else:
                scale_x = scale_y = 1.0
            cx = int(min(max(0, int(x) if x is not None else 0), sw or iw) * scale_x)
            cy = int(min(max(0, int(y) if y is not None else 0), sh or ih) * scale_y)
            draw = ImageDraw.Draw(img)
            size = max(20, int(min(iw, ih) * 0.03))
            thick = max(3, int(size * 0.2))
            # white border
            draw.line((cx - size, cy, cx + size, cy), fill=(255,255,255,255), width=thick+2)
            draw.line((cx, cy - size, cx, cy + size), fill=(255,255,255,255), width=thick+2)
            # red cross
            draw.line((cx - size, cy, cx + size, cy), fill=(255,0,0,255), width=thick)
            draw.line((cx, cy - size, cx, cy + size), fill=(255,0,0,255), width=thick)
            outp = img_path.replace('.png', '_tap.png')
            img.save(outp)
            print('Annotated tap image saved to', outp, flush=True)
        except Exception as e:
            print('Annotate image failed:', type(e).__name__, e, flush=True)
except Exception:
    pass
