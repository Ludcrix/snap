import os
import tempfile

def _split_text_to_chunks(text, max_words=6):
    words = text.split()
    chunks = []
    for i in range(0, len(words), max_words):
        chunk = words[i:i+max_words]
        chunks.append(' '.join(chunk))
    return chunks

def _format_srt_time(seconds):
    if seconds < 0:
        seconds = 0
    total_ms = int(round(seconds * 1000.0))
    hours = total_ms // 3_600_000
    total_ms %= 3_600_000
    minutes = total_ms // 60_000
    total_ms %= 60_000
    secs = total_ms // 1000
    ms = total_ms % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"

def generate_subtitles(
    text: str,
    audio_duration_seconds: float | None = None,
    start_offset_seconds: float = 0.0,
) -> str:
    chunks = _split_text_to_chunks(text, max_words=6)
    srt_lines = []
    try:
        start = float(start_offset_seconds or 0.0)
    except Exception:
        start = 0.0
    if start < 0:
        start = 0.0
    if audio_duration_seconds is not None:
        try:
            total = float(audio_duration_seconds)
        except Exception:
            total = None
    else:
        total = None

    if not chunks:
        # Still write a valid (empty) SRT file.
        chunks = []

    if total is not None and total > 0 and len(chunks) > 0:
        remaining = total - start
        if remaining <= 0:
            remaining = total
            start = 0.0
        per = remaining / float(len(chunks))
    else:
        per = 2.0  # fallback seconds per subtitle

    for idx, chunk in enumerate(chunks):
        end = start + per
        srt_lines.append(f"{idx+1}\n{_format_srt_time(start)} --> {_format_srt_time(end)}\n{chunk}\n")
        start = end
    srt_content = '\n'.join(srt_lines)
    subtitles_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), '..', 'storage', 'subtitles')
    os.makedirs(subtitles_dir, exist_ok=True)
    srt_file = f"subtitle_{next(tempfile._get_candidate_names())}.srt"
    srt_path = os.path.join(subtitles_dir, srt_file)
    with open(srt_path, 'w', encoding='utf-8') as f:
        f.write(srt_content)
    return os.path.abspath(srt_path)
