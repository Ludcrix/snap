"""Extended Telegram controller.

Runs the existing V1 controller but installs V2 format integrations at runtime.
This avoids modifying V1 source files while still extending the bot.

Usage:
  py -m bot.telegram_control_extended

V1 remains available as:
  py -m bot.telegram_control
"""

import bot.telegram_control as v1
from bot.formats.anomalie_objet.telegram_integration import install as install_ao


def run() -> None:
    install_ao(v1)

    # Mirror V1 __main__ behavior.
    with v1.STATE_LOCK:
        v1._load_state()
    v1._startup_ready_and_restore()
    v1.run()


if __name__ == "__main__":
  import os
  import sys
  if str(os.getenv("RUN_LEGACY_BOT", "")).strip() != "1":
    print("Legacy bot disabled. Use: py -m bot.v3.main (set RUN_LEGACY_BOT=1 to run legacy).", flush=True)
    sys.exit(2)
  run()
