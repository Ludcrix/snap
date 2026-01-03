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

# Determine a safe Y coordinate if stored value looks wrong
y = None
if 0 < y_px <= (sh if sh>0 else y_px):
    y = int(y_px)
else:
    # try dividing by common scales
    for div in (1,10,100,1000,10000):
        if y_px // div > 0 and (sh == 0 or (y_px // div) <= sh):
            y = int(y_px // div)
            break
    if y is None and isinstance(y_ratio, (int,float)) and sh>0 and 0 < float(y_ratio) <= 1.5:
        y = int(float(y_ratio) * sh)
    if y is None:
        # fallback to 10% from top
        y = int(max(10, (sh * 10)//100)) if sh>0 else 240

x = None
if 0 < x_px:
    # clamp large stored px by reducing scale until within width
    try:
        xi = int(x_px)
    except Exception:
        xi = None
    if xi and sw>0:
        while xi > sw and xi > 0:
            xi = xi // 10
        if xi <= 0:
            xi = None
    if xi:
        x = xi
    else:
        x = int(x_px)
elif isinstance(x_ratio, (int,float)) and sw>0:
    x = int(float(x_ratio) * sw)
else:
    x = 100

print(f"Using adb={adb}", flush=True)
print(f"Screen={sw}x{sh}", flush=True)
print(f"Using tap coords x={x} y={y}", flush=True)


cfg = AndroidAgentConfig(adb_path=adb, allow_input=True)
agent = AndroidAgent(cfg)


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


"""Teach step or skip behaviour.

If environment variable V3_SKIP_TEACH=1 is set, skip listening and use
the saved coordinates from `storage/v3/stv_click.json`.
"""
skip_teach = str(os.getenv('V3_SKIP_TEACH','')).strip() == '1'
if skip_teach:
    print('\n-- Skipping teach: using saved coordinates from stv_click.json --', flush=True)
    # Prefer stored raw ABS values if present and map them to pixels each run
    try:
        la_x = jd.get('last_abs_x')
        la_y = jd.get('last_abs_y')
        a_min_x = jd.get('abs_min_x')
        a_max_x = jd.get('abs_max_x')
        a_min_y = jd.get('abs_min_y')
        a_max_y = jd.get('abs_max_y')
        if la_x is not None and la_y is not None and a_min_x is not None and a_max_x is not None and a_max_x > a_min_x:
            try:
                mapped_x = int((int(la_x) - int(a_min_x)) * float(sw) / float(int(a_max_x) - int(a_min_x)))
            except Exception:
                mapped_x = x
        else:
            mapped_x = x
        if la_y is not None and la_y is not None and a_min_y is not None and a_max_y is not None and a_max_y > a_min_y:
            try:
                mapped_y = int((int(la_y) - int(a_min_y)) * float(sh) / float(int(a_max_y) - int(a_min_y)))
            except Exception:
                mapped_y = y
        else:
            mapped_y = y
        # clamp
        if sw>0:
            mapped_x = max(0, min(sw, mapped_x))
        if sh>0:
            mapped_y = max(0, min(sh, mapped_y))
        x = mapped_x
        y = mapped_y
        print(f'Using mapped coords from stored ABS: x={x} y={y} (raw {la_x},{la_y})', flush=True)
        use_coords = True
    except Exception:
        print(f'Using tap coords x={x} y={y}', flush=True)
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
        # mapping ABS -> px
        try:
            gx_i = int(gx)
        except Exception:
            gx_i = None
        try:
            gy_i = int(gy)
        except Exception:
            gy_i = None

        mapped_x = None
        mapped_y = None
        if gx_i is not None and sw>0 and min_x is not None and max_x is not None and max_x > min_x:
            try:
                mapped_x = int((gx_i - min_x) * float(sw) / float(max_x - min_x))
            except Exception:
                mapped_x = None
        if gy_i is not None and sh>0 and min_y is not None and max_y is not None and max_y > min_y:
            try:
                mapped_y = int((gy_i - min_y) * float(sh) / float(max_y - min_y))
            except Exception:
                mapped_y = None

        # fallback: if mapping not possible, try reducing scale by /10 heuristics
        if mapped_x is None:
            if gx_i:
                gx_tmp = gx_i
                while sw>0 and gx_tmp > sw and gx_tmp > 0:
                    gx_tmp = gx_tmp // 10
                mapped_x = int(gx_tmp)
            else:
                mapped_x = x
        if mapped_y is None:
            if gy_i:
                gy_tmp = gy_i
                while sh>0 and gy_tmp > sh and gy_tmp > 0:
                    gy_tmp = gy_tmp // 10
                mapped_y = int(gy_tmp)
            else:
                mapped_y = y

        # clamp to screen
        if sw>0:
            mapped_x = max(0, min(sw, mapped_x))
        if sh>0:
            mapped_y = max(0, min(sh, mapped_y))

        x = mapped_x
        y = mapped_y
        use_coords = True
        # persist into stv_click.json
        try:
            cur = {}
            if CLICK.exists():
                cur = json.loads(CLICK.read_text(encoding='utf-8') or '{}')
            cur.update({'adb': adb, 'x_px': x, 'y_px': y})
            # also write ratios if screen known
            if sw>0 and sh>0:
                cur['screen_w'] = sw
                cur['screen_h'] = sh
                cur['x_ratio'] = float(x)/float(sw)
                cur['y_ratio'] = float(y)/float(sh)
            CLICK.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding='utf-8')
            print('Saved coords to', CLICK, flush=True)
        except Exception as e:
            print('Failed to persist coords:', e, flush=True)
    else:
        print('No coords detected in getevent output.', flush=True)


print("Tapping once then capturing 3 screenshots (0.5s pause)", flush=True)
base = agent._adb_base()
logs = []
images = []
ocr_texts = []
parsed = None
try:
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
