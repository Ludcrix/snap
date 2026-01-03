from __future__ import annotations

from dataclasses import dataclass
import subprocess
import time


@dataclass(frozen=True)
class AndroidControllerConfig:
    adb_path: str = "adb"
    serial: str | None = None


class AndroidController:
    """Thin ADB wrapper.

    V3 rule: Android device stores no critical data.
    V3 state is persisted on the host immediately.
    """

    def __init__(self, cfg: AndroidControllerConfig):
        self._cfg = cfg

    def _adb_base(self) -> list[str]:
        cmd = [self._cfg.adb_path]
        if self._cfg.serial:
            cmd += ["-s", self._cfg.serial]
        return cmd

    def _run(self, args: list[str], *, timeout: float = 15.0) -> str:
        cmd = self._adb_base() + args
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            raise RuntimeError(f"adb failed ({p.returncode}): {p.stderr.strip() or p.stdout.strip()}")
        return (p.stdout or "").strip()

    def is_connected(self) -> bool:
        try:
            out = self._run(["get-state"], timeout=5.0)
            return "device" in out.lower()
        except Exception:
            return False

    def swipe_scroll(self) -> None:
        # Generic scroll gesture; app must already be focused.
        # Coordinates are conservative for phones.
        self._run(["shell", "input", "swipe", "500", "1600", "500", "500", "300"], timeout=10.0)

    def tap(self, x: int, y: int) -> None:
        self._run(["shell", "input", "tap", str(int(x)), str(int(y))], timeout=5.0)

    def pause(self, seconds: float) -> None:
        time.sleep(max(0.0, float(seconds)))

    def like_current(self) -> None:
        """Best-effort like action.

        IMPORTANT: must only be called after human approval.
        This is app-UI dependent; kept as a simple double-tap gesture.
        """

        # Double-tap center area.
        self.tap(540, 960)
        time.sleep(0.1)
        self.tap(540, 960)
