from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import glob

from .dotenv import load_v3_dotenv


@dataclass(frozen=True)
class V3Config:
    # Storage
    data_dir: Path
    state_file: Path

    # Android / ADB
    adb_path: str
    adb_serial: str | None

    # Device-visible actions (ADB input) — opt-in
    enable_device_input: bool
    instagram_package: str
    swipe_duration_ms: int
    swipe_margin_ratio: float
    tap_to_open: bool

    # Telegram
    telegram_token: str
    telegram_allowed_chat_ids: set[int]  # empty = allow all (not recommended)

    # Session loop
    step_sleep_seconds: float

    # Step2: safety controls
    max_session_seconds: float
    risk_alert_cooldown_seconds: float


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


def load_v3_config() -> V3Config:
    root = Path(__file__).resolve().parents[2]

    # Best-effort: load project .env files (V1/V2 style) so V3 can run without extra setup.
    load_v3_dotenv(root=root)

    data_dir = Path(_env("V3_DATA_DIR", str(root / "storage" / "v3"))).resolve()
    state_file = Path(_env("V3_STATE_FILE", str(data_dir / "state.json"))).resolve()

    # ADB is an external binary. Prefer explicit env override, otherwise try to resolve.
    adb_path = _env("V3_ADB_PATH", "").strip()
    if not adb_path:
        # Try PATH first.
        found = shutil.which("adb")
        if found:
            adb_path = found
        else:
            # Windows winget install location (best-effort):
            # %LOCALAPPDATA%\Microsoft\WinGet\Packages\Google.PlatformTools_*\platform-tools\adb.exe
            local = os.environ.get("LOCALAPPDATA", "")
            if local:
                pattern = str(Path(local) / "Microsoft" / "WinGet" / "Packages" / "Google.PlatformTools_*" / "platform-tools" / "adb.exe")
                matches = sorted(glob.glob(pattern))
                if matches:
                    adb_path = matches[-1]

    if not adb_path:
        adb_path = "adb"
    adb_serial = _env("V3_ADB_SERIAL", "") or None

    # IMPORTANT: device input is disabled by default.
    enable_device_input = _env("V3_ENABLE_DEVICE_INPUT", "0").strip() in {"1", "true", "True", "yes", "YES"}
    instagram_package = _env("V3_INSTAGRAM_PACKAGE", "com.instagram.android") or "com.instagram.android"
    try:
        swipe_duration_ms = int(float(_env("V3_SWIPE_DURATION_MS", "420")))
    except Exception:
        swipe_duration_ms = 420
    try:
        swipe_margin_ratio = float(_env("V3_SWIPE_MARGIN_RATIO", "0.18"))
    except Exception:
        swipe_margin_ratio = 0.18
    swipe_margin_ratio = max(0.05, min(0.45, swipe_margin_ratio))
    tap_to_open = _env("V3_TAP_TO_OPEN", "1").strip() in {"1", "true", "True", "yes", "YES"}

    telegram_token = _env("TELEGRAM_BOT_TOKEN", "")
    if not telegram_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing (required for V3 Telegram UI)")

    raw_allowed = _env("V3_TELEGRAM_ALLOWED_CHAT_IDS", "")
    allowed: set[int] = set()
    for part in [p.strip() for p in raw_allowed.split(",") if p.strip()]:
        try:
            allowed.add(int(part))
        except Exception:
            continue

    try:
        step_sleep_seconds = float(_env("V3_STEP_SLEEP_SECONDS", "2.0"))
    except Exception:
        step_sleep_seconds = 2.0

    try:
        max_session_seconds = float(_env("V3_MAX_SESSION_SECONDS", str(15 * 60)))
    except Exception:
        max_session_seconds = float(15 * 60)

    try:
        risk_alert_cooldown_seconds = float(_env("V3_RISK_ALERT_COOLDOWN_SECONDS", "60"))
    except Exception:
        risk_alert_cooldown_seconds = 60.0

    return V3Config(
        data_dir=data_dir,
        state_file=state_file,
        adb_path=adb_path,
        adb_serial=adb_serial,
        enable_device_input=enable_device_input,
        instagram_package=instagram_package,
        swipe_duration_ms=swipe_duration_ms,
        swipe_margin_ratio=swipe_margin_ratio,
        tap_to_open=tap_to_open,
        telegram_token=telegram_token,
        telegram_allowed_chat_ids=allowed,
        step_sleep_seconds=step_sleep_seconds,
        max_session_seconds=max_session_seconds,
        risk_alert_cooldown_seconds=risk_alert_cooldown_seconds,
    )
