import os
from pathlib import Path

from dotenv import load_dotenv


def _extract_dotenv_value(dotenv_file_path: str, key: str) -> str | None:
    # Tolerant KEY=value extractor (mirrors V1 behavior, but local to this format).
    try:
        text = Path(dotenv_file_path).read_text(encoding="utf-8")
    except Exception:
        return None

    needle = f"{key}="
    idx = text.find(needle)
    if idx == -1:
        return None

    start = idx + len(needle)
    tail = text[start:]
    line = tail.splitlines()[0] if tail else ""
    value = line.strip()

    if len(value) >= 2 and ((value[0] == value[-1] == '"') or (value[0] == value[-1] == "'")):
        value = value[1:-1].strip()
    return value or None


def load_anomalie_objet_dotenv() -> str | None:
    """Load env vars for this format from a duplicated dotenv file.

    By default we look for project-root `.env.anomalie_objet`.
    We use override=True so this format can run with a different token/key
    without changing the V1 `.env`.

    Returns the path loaded (or None if not found).
    """

    root = Path(__file__).resolve().parents[3]
    dotenv_path = root / ".env.anomalie_objet"
    if not dotenv_path.exists():
        return None

    load_dotenv(dotenv_path=str(dotenv_path), override=True)

    # Fallback for malformed lines.
    for key in ["OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN"]:
        if not os.getenv(key):
            v = _extract_dotenv_value(str(dotenv_path), key)
            if v:
                os.environ[key] = v

    return str(dotenv_path)
