import os
from dotenv import load_dotenv


def _extract_dotenv_value(dotenv_file_path: str, key: str) -> str | None:
    """Best-effort extraction of KEY=value from a dotenv file.

    This is intentionally tolerant: it also handles malformed lines where the key
    is not at the beginning of the line (e.g. "...activateOPENAI_API_KEY=...").
    """
    try:
        with open(dotenv_file_path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return None

    needle = f"{key}="
    idx = text.find(needle)
    if idx == -1:
        return None

    start = idx + len(needle)
    # Read until end-of-line.
    tail = text[start:]
    line = tail.splitlines()[0] if tail else ""
    value = line.strip()

    # Strip optional surrounding quotes.
    if len(value) >= 2 and ((value[0] == value[-1] == '"') or (value[0] == value[-1] == "'")):
        value = value[1:-1].strip()
    return value or None


dotenv_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
load_dotenv(dotenv_path=dotenv_path)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MODEL_NAME = "gpt-4.1-mini"

# Fallback for malformed .env lines (do not modify the user's .env file).
if not OPENAI_API_KEY:
    v = _extract_dotenv_value(dotenv_path, "OPENAI_API_KEY")
    if v:
        os.environ["OPENAI_API_KEY"] = v
        OPENAI_API_KEY = v

if not ELEVENLABS_API_KEY:
    v = _extract_dotenv_value(dotenv_path, "ELEVENLABS_API_KEY")
    if v:
        os.environ["ELEVENLABS_API_KEY"] = v
        ELEVENLABS_API_KEY = v

if not TELEGRAM_BOT_TOKEN:
    v = _extract_dotenv_value(dotenv_path, "TELEGRAM_BOT_TOKEN")
    if v:
        os.environ["TELEGRAM_BOT_TOKEN"] = v
        TELEGRAM_BOT_TOKEN = v

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is missing in .env file")

if not ELEVENLABS_API_KEY:
    raise RuntimeError("ELEVENLABS_API_KEY is missing in .env file")
