from __future__ import annotations

from pathlib import Path
import json
import threading
from typing import Callable, TypeVar

from .state import V3State, dict_to_state, state_to_dict


_LOCK = threading.Lock()


T = TypeVar("T")


def ensure_parent_dir(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def load_state(state_file: Path) -> V3State:
    ensure_parent_dir(state_file)
    if not state_file.exists():
        return V3State()
    try:
        raw = state_file.read_text(encoding="utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return V3State()
        return dict_to_state(payload)
    except Exception:
        return V3State()


def save_state(state_file: Path, st: V3State) -> None:
    ensure_parent_dir(state_file)
    payload = state_to_dict(st)
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(state_file)


def load_state_locked(state_file: Path) -> V3State:
    with _LOCK:
        return load_state(state_file)


def save_state_locked(state_file: Path, st: V3State) -> None:
    with _LOCK:
        save_state(state_file, st)


def update_state_locked(state_file: Path, updater: Callable[[V3State], T]) -> T:
    """Atomically load->mutate->save under the same lock.

    This prevents lost updates when multiple threads update different fields.
    """
    with _LOCK:
        st = load_state(state_file)
        out = updater(st)
        save_state(state_file, st)
        return out
