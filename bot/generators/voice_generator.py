import os
import json
import urllib.request
import urllib.error
import re
import wave
from typing import Tuple

from ..config import ELEVENLABS_API_KEY


VOICE_LEAD_IN_SILENCE_SECONDS = 0.35


def _resolve_elevenlabs_voice_id(gender: str | None) -> str:
    """Resolve a voice_id deterministically.

    Requirement: select a specific voice_id based on voice.gender.
    We do NOT fall back to a default voice (no /v1/voices lookup).
    """
    gender_lc = (gender or "").strip().lower()
    if gender_lc == "female":
        env_id = os.getenv("ELEVENLABS_VOICE_ID_FEMALE")
        which = "ELEVENLABS_VOICE_ID_FEMALE"
    elif gender_lc == "male":
        env_id = os.getenv("ELEVENLABS_VOICE_ID_MALE")
        which = "ELEVENLABS_VOICE_ID_MALE"
    else:
        raise ValueError("voice.gender must be 'male' or 'female' to select an ElevenLabs voice_id")

    if not env_id or not env_id.strip():
        raise RuntimeError(
            f"Missing required {which}. Set it to a specific ElevenLabs voice_id (no default voice allowed)."
        )
    return env_id.strip()


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def _voice_settings_from_style(voice_style: dict | None) -> dict:
    # ElevenLabs voice_settings: stability, similarity_boost, style, use_speaker_boost
    # Requirements:
    # - stability between 0.25 and 0.45
    # - similarity_boost between 0.65 and 0.85
    # - style between 0.5 and 0.8
    # - use_speaker_boost = true
    tone = None
    pace = None
    energy = None
    pitch = None
    if isinstance(voice_style, dict):
        tone = voice_style.get("tone")
        pace = voice_style.get("pace")
        energy = voice_style.get("energy")
        pitch = voice_style.get("pitch")

    # Base values chosen inside required ranges.
    stability = 0.35
    similarity = 0.75
    style = 0.65

    if tone == "calm":
        stability = 0.43
        style = 0.58
    elif tone == "tense":
        stability = 0.28
        style = 0.78
    elif tone == "intimate":
        stability = 0.32
        style = 0.72

    if energy == "low":
        style -= 0.06
    elif energy == "medium":
        style += 0.03

    # pace/pitch: keep as text formatting (pauses, line breaks, ellipses).
    _ = pace, pitch

    return {
        "stability": _clamp(stability, 0.25, 0.45),
        "similarity_boost": _clamp(similarity, 0.65, 0.85),
        "style": _clamp(style, 0.50, 0.80),
        "use_speaker_boost": True,
    }


def _rewrite_voice_script_spoken_french(text: str) -> str:
    """Rewrite/format a script to sound more like spoken French.

    - Very short sentences
    - Sentence fragments allowed
    - Pauses via line breaks and ellipses
    - Avoid overly formal formatting (we don't add literary flourishes)

    We do a deterministic, local transformation (no provider/LLM change).
    """
    if not isinstance(text, str):
        return ""
    t = text.strip()
    if not t:
        return ""

    # Normalize whitespace but preserve intentional line breaks if present.
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)

    # Make punctuation more "spoken".
    t = t.replace(";", ".")
    t = t.replace(":", ".")

    # Split into phrases on strong punctuation.
    parts = re.split(r"(?<=[\.!\?…])\s+", t)
    phrases: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # Further split long phrases on commas / dashes.
        sub = re.split(r"\s*(?:,|—|-)\s+", p)
        for s in sub:
            s = s.strip()
            if not s:
                continue
            phrases.append(s)

    # Hard-limit phrase length by chunking into short word groups.
    lines: list[str] = []
    for phrase in phrases:
        words = phrase.split()
        if len(words) <= 8:
            lines.append(phrase)
            continue
        # Chunk into short fragments (spoken rhythm).
        for i in range(0, len(words), 6):
            chunk = " ".join(words[i : i + 6]).strip()
            if chunk:
                lines.append(chunk)

    # Insert ellipses pauses between beats.
    out: list[str] = []
    for i, line in enumerate(lines):
        out.append(line)
        # Pause every ~2 lines unless it's already ending with a pause.
        if i < len(lines) - 1:
            if not re.search(r"(\.\.\.|…)$", line.strip()):
                if (i + 1) % 2 == 0:
                    out.append("...")

    # Cleanup: collapse too many ellipsis lines.
    spoken = "\n".join(out)
    spoken = re.sub(r"(?:\n\.\.\.){3,}", "\n...", spoken)
    spoken = re.sub(r"\n{3,}", "\n\n", spoken).strip()
    return spoken


def _write_wav_from_pcm(out_path: str, pcm_bytes: bytes, sample_rate: int = 16000, channels: int = 1) -> None:
    # ElevenLabs PCM outputs are 16-bit little-endian.
    sampwidth = 2
    frame_size = channels * sampwidth
    if frame_size <= 0:
        raise ValueError("Invalid frame size")
    if len(pcm_bytes) % frame_size != 0:
        pcm_bytes = pcm_bytes[: len(pcm_bytes) - (len(pcm_bytes) % frame_size)]
    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)


def _is_valid_wav_header(raw: bytes) -> bool:
    return isinstance(raw, (bytes, bytearray)) and len(raw) >= 12 and raw[0:4] == b"RIFF" and raw[8:12] == b"WAVE"


def _prepend_silence_inplace(wav_path: str, silence_seconds: float) -> None:
    if silence_seconds <= 0:
        return

    tmp_path = wav_path + ".tmp"
    with wave.open(wav_path, "rb") as r:
        n_channels = r.getnchannels()
        sampwidth = r.getsampwidth()
        framerate = r.getframerate()
        n_frames = r.getnframes()
        audio_frames = r.readframes(n_frames)

    silence_frames = int(round(silence_seconds * float(framerate)))
    if silence_frames <= 0:
        return

    silence_bytes = (b"\x00" * sampwidth) * n_channels * silence_frames

    with wave.open(tmp_path, "wb") as w:
        w.setnchannels(n_channels)
        w.setsampwidth(sampwidth)
        w.setframerate(framerate)
        w.writeframes(silence_bytes)
        w.writeframes(audio_frames)

    os.replace(tmp_path, wav_path)


def get_wav_duration_seconds(wav_path: str) -> float:
    if not isinstance(wav_path, str) or not wav_path.strip():
        raise ValueError("wav_path must be a non-empty string")
    with wave.open(wav_path, "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        if rate <= 0:
            return 0.0
        return frames / float(rate)


def generate_voice_with_duration(voice_script: str, voice_style: dict | None = None, log_fn=None) -> Tuple[str, float]:
    def _log(msg: str) -> None:
        if callable(log_fn):
            log_fn("VOICE", msg)

    if not isinstance(voice_script, str) or not voice_script.strip():
        raise ValueError("voice_script must be a non-empty string")

    if isinstance(voice_style, dict):
        _log(f"Voice style selected: {voice_style}")
    _log("Voice script ready")
    _log("Using ElevenLabs Text-to-Speech")

    audio_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "storage", "audio")
    os.makedirs(audio_dir, exist_ok=True)
    wav_path = os.path.abspath(os.path.join(audio_dir, "voice.wav"))

    gender = voice_style.get("gender") if isinstance(voice_style, dict) else None
    voice_id = _resolve_elevenlabs_voice_id(gender)
    voice_settings = _voice_settings_from_style(voice_style)
    _log(f"ElevenLabs voice_id used: {voice_id}")
    _log(f"ElevenLabs voice settings: {voice_settings}")

    # Rewrite/format the script for more natural spoken French delivery.
    voice_script = _rewrite_voice_script_spoken_french(voice_script)
    model_id = os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")

    # Request PCM and wrap as WAV locally for consistent single-file output.
    output_format = os.getenv("ELEVENLABS_OUTPUT_FORMAT", "pcm_16000")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format={output_format}"
    payload = {
        "text": voice_script,
        "model_id": model_id,
        "language_code": "fr",
        "voice_settings": voice_settings,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            audio_bytes = resp.read()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        if e.code == 401:
            key_len = len(ELEVENLABS_API_KEY or "")
            raise RuntimeError(
                "ElevenLabs Text-to-Speech authentication failed (HTTP 401 Unauthorized). "
                "The server rejected ELEVENLABS_API_KEY. "
                "Double-check the key value (no extra quotes/spaces) and account permissions. "
                f"(key length={key_len}) Response body: {body}"
            )
        raise RuntimeError(f"ElevenLabs TTS failed: HTTP {e.code} {e.reason} {body}")

    if not audio_bytes:
        raise RuntimeError("ElevenLabs TTS returned empty audio")

    # If API returns WAV bytes, save directly; otherwise assume PCM and wrap.
    if _is_valid_wav_header(audio_bytes):
        with open(wav_path, "wb") as f:
            f.write(audio_bytes)
    else:
        # Default ElevenLabs PCM output: 16kHz mono, 16-bit little-endian.
        _write_wav_from_pcm(wav_path, pcm_bytes=audio_bytes, sample_rate=16000, channels=1)

    # Add a short silence before the voice starts (0.3–0.5s).
    _prepend_silence_inplace(wav_path, silence_seconds=VOICE_LEAD_IN_SILENCE_SECONDS)

    _log("Audio generated (.wav)")
    duration = get_wav_duration_seconds(wav_path)
    _log(f"Audio duration: {duration:.2f}s")
    return wav_path, duration

def generate_voice(voice_script: str, log_fn=None) -> str:
    wav_path, _duration = generate_voice_with_duration(voice_script, voice_style=None, log_fn=log_fn)
    return wav_path
