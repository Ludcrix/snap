"""Project entrypoint (disabled).

To avoid running multiple instances / duplicate actions, V3 must be launched
only via:
  py -m bot.v3.main

Legacy Telegram controllers remain available (guarded by RUN_LEGACY_BOT=1):
  - py -m bot.telegram_control
  - py -m bot.telegram_control_extended
  - py -m bot.formats.anomalie_objet.telegram_control
"""


if __name__ == "__main__":
    import sys

    print("Entry point disabled. Use: py -m bot.v3.main", flush=True)
    sys.exit(2)
