import os
import sys
import subprocess
from pathlib import Path

OUT_DIR = Path('storage') / 'v3'
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Coordinates and colors to mark
POINTS = [
    (100, 100, (255, 0, 0, 255), 'red'),
    (1650, 100, (0, 120, 255, 255), 'blue'),
    (100, 2700, (255, 140, 0, 255), 'orange'),
    (1650, 2700, (204, 255, 0, 255), 'yellow')
]


def find_adb():
    # prefer adb path from stored stv_click.json if present
    try:
        import json
        p = Path('storage') / 'v3' / 'stv_click.json'
        if p.exists():
            jd = json.loads(p.read_text(encoding='utf-8') or '{}')
            adb = jd.get('adb')
            if adb:
                return str(adb)
    except Exception:
        pass
    # fallback to PATH
    return os.environ.get('V3_ADB_PATH') or 'adb'


def capture_screenshot(adb):
    try:
        cp = subprocess.run([adb, 'exec-out', 'screencap', '-p'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
        if int(getattr(cp, 'returncode', 1)) != 0:
            print('screencap failed', file=sys.stderr)
            return None
        return cp.stdout
    except Exception as e:
        print('screencap exception:', e, file=sys.stderr)
        return None


def save_bytes(data, path: Path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)
        return True
    except Exception as e:
        print('save failed:', e, file=sys.stderr)
        return False


def mark_point_on_image(data_bytes, point, out_path: Path):
    try:
        from PIL import Image, ImageDraw
    except Exception:
        print('Pillow not installed. Install with: pip install pillow', file=sys.stderr)
        return False

    try:
        import io
        img = Image.open(io.BytesIO(data_bytes)).convert('RGBA')
        draw = ImageDraw.Draw(img)
        iw, ih = img.size
        x, y, rgba, _name = point
        # clamp provided coords to image bounds
        cx = max(0, min(iw - 1, int(x)))
        cy = max(0, min(ih - 1, int(y)))
        size = max(20, int(min(iw, ih) * 0.03))
        thick = max(2, int(size * 0.18))
        # draw outer white border
        draw.line((cx - size, cy, cx + size, cy), fill=(255, 255, 255, 255), width=thick + 2)
        draw.line((cx, cy - size, cx, cy + size), fill=(255, 255, 255, 255), width=thick + 2)
        # draw colored cross
        draw.line((cx - size, cy, cx + size, cy), fill=rgba, width=thick)
        draw.line((cx, cy - size, cx, cy + size), fill=rgba, width=thick)
        # small filled circle center
        r = max(6, int(size * 0.12))
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=rgba)
        img.save(out_path)
        return True
    except Exception as e:
        print('marking failed:', e, file=sys.stderr)
        return False


def main():
    adb = find_adb()
    print('Using adb=', adb)
    saved = []
    for i, point in enumerate(POINTS):
        data = capture_screenshot(adb)
        if not data:
            print('Failed to capture screenshot for point', i)
            continue
        fname = OUT_DIR / f'test_point_{i}.png'
        save_bytes(data, fname)
        outp = OUT_DIR / f'test_point_{i}_marked.png'
        ok = mark_point_on_image(data, point, outp)
        if ok:
            print('Saved marked image:', outp)
            saved.append(str(outp))
            try:
                # open image with default viewer on Windows
                if sys.platform.startswith('win'):
                    os.startfile(str(outp))
            except Exception:
                pass
        else:
            print('Saved raw screenshot:', fname)

    if not saved:
        print('No images saved/marked.')
    else:
        print('\nDone. Marked images:')
        for s in saved:
            print('-', s)


if __name__ == '__main__':
    main()
