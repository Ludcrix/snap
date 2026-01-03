from __future__ import annotations

from pathlib import Path


def _format_srt_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    ms = int(round(seconds * 1000.0))
    h = ms // 3_600_000
    ms -= h * 3_600_000
    m = ms // 60_000
    ms -= m * 60_000
    s = ms // 1000
    ms -= s * 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_one_line_srt(*, text: str, out_dir: Path, duration_seconds: float) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    start = _format_srt_time(0.0)
    end = _format_srt_time(max(0.5, float(duration_seconds)))
    payload = f"1\n{start} --> {end}\n{text.strip()}\n"
    out_path = out_dir / "subtitle.srt"
    out_path.write_text(payload, encoding="utf-8")
    return str(out_path)
