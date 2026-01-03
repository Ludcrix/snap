import json
import os
import time
import subprocess
import threading
import queue
from pathlib import Path

CLICK = Path('storage/v3/stv_click.json')
CLICK.parent.mkdir(parents=True, exist_ok=True)

def try_parse_num(tok: str):
    tok = str(tok).strip()
    if not tok:
        raise ValueError('empty')
    try:
        return int(tok, 0)
    except Exception:
        pass
    try:
        return int(tok, 16)
    except Exception:
        pass
    import re
    m = re.search(r"[0-9a-fA-F]+", tok)
    if m:
        return int(m.group(0), 16 if any(c in 'abcdefABCDEF' for c in m.group(0)) else 10)
    raise ValueError(f'cannot parse num: {tok}')

def listen_getevent(adb_cmd='adb', timeout=10.0):
    lines = []
    last_x = last_y = None
    min_x = max_x = min_y = max_y = None
    try:
        proc = subprocess.Popen([adb_cmd, 'shell', 'getevent', '-lt'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except Exception as e:
        return lines, None, None, (None,None,None,None), f'start_exc:{e}'

    q = queue.Queue()
    def reader():
        try:
            for ln in proc.stdout:
                q.put(ln)
        except Exception:
            pass
    t = threading.Thread(target=reader, daemon=True)
    t.start()

    import re, time
    end = time.time() + float(timeout)
    try:
        while time.time() < end:
            try:
                ln = q.get(timeout=0.25)
            except queue.Empty:
                continue
            s = ln.rstrip('\n')
            lines.append(s)
            l = s.strip()
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
                            else:
                                last_y = v
                            if min_x is None or v < min_x:
                                min_x = v
                            if max_x is None or v > max_x:
                                max_x = v
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

def find_adb():
    import shutil, glob
    # 1) env var
    adb = os.getenv('V3_ADB_PATH')
    if adb and os.path.exists(adb):
        return adb
    # 2) existing CLICK file value
    try:
        if CLICK.exists():
            jd = json.loads(CLICK.read_text(encoding='utf-8') or '{}')
            cand = jd.get('adb')
            if cand and os.path.exists(cand) and os.path.isfile(cand):
                return cand
    except Exception:
        pass
    # 3) PATH
    candidate = shutil.which('adb')
    if candidate:
        return candidate
    # 4) WinGet default location pattern
    local = os.getenv('LOCALAPPDATA')
    if local:
        pattern = os.path.join(local, 'Microsoft', 'WinGet', 'Packages', 'Google.PlatformTools_*', 'platform-tools', 'adb.exe')
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return 'adb'


def main():
    adb = find_adb()
    print('Listening 10s for stylus tap (use stylus on device)...', flush=True)
    lines, lx, ly, ranges, err = listen_getevent(adb_cmd=adb, timeout=10.0)
    ts = int(time.time())
    logp = CLICK.parent / f'stv_getevent_{ts}.log'
    try:
        with open(logp, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
    except Exception:
        pass
    print('Saved getevent log to', logp, flush=True)
    print('Detected raw ABS:', lx, ly, 'ranges:', ranges, 'err:', err, flush=True)
    cur = {}
    if CLICK.exists():
        try:
            cur = json.loads(CLICK.read_text(encoding='utf-8') or '{}')
        except Exception:
            cur = {}
    if lx is not None:
        cur['last_abs_x'] = lx
    if ly is not None:
        cur['last_abs_y'] = ly
    if ranges and len(ranges) == 4:
        a,b,c,d = ranges
        cur['abs_min_x'] = a
        cur['abs_max_x'] = b
        cur['abs_min_y'] = c
        cur['abs_max_y'] = d
    # Preserve existing adb unless we found a better path
    if not cur.get('adb') or cur.get('adb') == 'adb' or (adb and adb != 'adb'):
        cur['adb'] = adb
    cur['created'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    CLICK.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Saved stv_click.json with raw ABS and ranges.', flush=True)

if __name__ == '__main__':
    main()
