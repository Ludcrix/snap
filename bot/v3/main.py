from __future__ import annotations

"""V3 entry point.

Run:
  py -m bot.v3.main

Useful env vars:
  - V3_ENABLE_DEVICE_INPUT=1  (enable visible ADB tap/swipe)
  - V3_ANDROID_DEBUG=1        (verbose Android/ADB + share-sheet diagnostics)

V3 is isolated; does not modify V1/V2.
"""

import os
import sys
from pathlib import Path
import hashlib


def _acquire_single_instance_lock() -> None:
  """Prevent running multiple V3 processes concurrently.

  Use a Windows named mutex (robust across terminals and working directories).
  If another instance already holds it, we exit immediately.
  """
  # Scope the mutex by state file path (if configured) so different state files
  # can run independently, while the common case stays single-instance.
  state_file = str(os.getenv("V3_STATE_FILE", "")).strip()
  state_key = str(Path(state_file).resolve()) if state_file else "default"
  suffix = hashlib.sha1(state_key.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
  mutex_name = f"Global\\snap-bot-v3-main-{suffix}"

  try:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    CreateMutexW = kernel32.CreateMutexW
    CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    CreateMutexW.restype = wintypes.HANDLE
    GetLastError = kernel32.GetLastError

    handle = CreateMutexW(None, True, mutex_name)
    if not handle:
      raise OSError("CreateMutexW failed")

    ERROR_ALREADY_EXISTS = 183
    if int(GetLastError()) == ERROR_ALREADY_EXISTS:
      print(
        f"[V3][LOCK] Another instance is already running; refusing to start. mutex={mutex_name}",
        file=sys.stderr,
        flush=True,
      )
      raise SystemExit(2)

    # Keep handle alive for lifetime of the process.
    globals()["_V3_MUTEX_HANDLE"] = handle
  except SystemExit:
    raise
  except Exception as e:
    # If mutex acquisition fails for any reason, fail closed to avoid multi-instance corruption.
    print(f"[V3][LOCK] Failed to acquire mutex: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    raise SystemExit(2)


def _run_v3() -> None:
  # Import after lock so a second instance cannot partially start threads.
  from .telegram.integration import run

  run()


if __name__ == "__main__":
  _acquire_single_instance_lock()
  _run_v3()
