from __future__ import annotations

"""ADB/UIAutomator probe for ad markers on Instagram Reels.

Purpose:
- Debug what the tablet "remonte" (UI hierarchy) when a sponsored reel is visible.
- Does NOT start the full Telegram bot loop; no mutex; no device input.

Run:
  py -m bot.v3.ad_probe

Optional env vars:
  - V3_ADB_PATH=adb
  - V3_ANDROID_SERIAL=<device-serial>
  - V3_ANDROID_DEBUG=1   (recommended)

Tips:
- Put the tablet on a sponsored reel, then run this probe.
- It will loop until it sees sponsor-ish markers, printing debug lines.
"""

import os
import time

from .android_agent import AndroidAgent, AndroidAgentConfig


def _make_agent() -> AndroidAgent:
    adb_path = str(os.getenv("V3_ADB_PATH", "adb")).strip() or "adb"
    serial = str(os.getenv("V3_ANDROID_SERIAL", "")).strip() or None
    cfg = AndroidAgentConfig(adb_path=adb_path, serial=serial, allow_input=False)
    return AndroidAgent(cfg)


def main() -> int:
    # Force helpful logs by default for this probe.
    if str(os.getenv("V3_ANDROID_DEBUG", "")).strip() != "1":
        os.environ["V3_ANDROID_DEBUG"] = "1"

    agent = _make_agent()

    print("[AD_PROBE] Starting. Put Instagram on a sponsored reel.", flush=True)
    print("[AD_PROBE] Looping dumps every 400ms until marker appears...", flush=True)

    # We detect via the same production heuristic.
    # When it returns False but sponsor is present, android_agent will now print a hint.
    for i in range(1, 401):
        is_ad = bool(agent.is_probably_ad_reel())
        print(f"[AD_PROBE] tick={i} is_ad={is_ad}", flush=True)
        if is_ad:
            print("[AD_PROBE] DETECTED ad reel", flush=True)
            return 0
        time.sleep(0.4)

    print("[AD_PROBE] ❌ No ad detected within ~160s", flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
