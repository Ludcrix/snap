from __future__ import annotations

from pathlib import Path
import os


def _strip_quotes(v: str) -> str:
    v = v.strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    return v


def load_dotenv_file(path: Path, *, override: bool = False) -> bool:
    """Load KEY=VALUE pairs into os.environ.

    - Minimal parser (no expansions).
    - Ignores empty lines and comments.
    - If override=False, keeps existing env vars.

    Returns True if file existed and was parsed.
    """

    try:
        if not path.exists() or not path.is_file():
            return False
    except Exception:
        return False

    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return False

    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        k, v = s.split("=", 1)
        key = k.strip()
        if not key:
            continue
        val = _strip_quotes(v)
        if (not override) and (key in os.environ) and str(os.environ.get(key) or ""):
            continue
        os.environ[key] = val

    return True


def load_v3_dotenv(*, root: Path) -> None:
    """Best-effort dotenv loading for V3.

    Does not affect V1/V2 code; just ensures V3 can run when tokens are stored in .env files.
    """

    candidates = [
        root / ".env",
        # V2 format-specific env (commonly contains TELEGRAM_BOT_TOKEN)
        root / ".env.anomalie_objet",
    ]

    for p in candidates:
        load_dotenv_file(p, override=False)
