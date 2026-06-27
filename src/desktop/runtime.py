"""Shared desktop runtime state and emergency stop helpers."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from time import time


@dataclass(frozen=True)
class DesktopRuntimeSnapshot:
    stopped: bool = False
    reason: str | None = None
    stopped_at: float | None = None


class DesktopRuntimeStoppedError(RuntimeError):
    """Raised when a desktop control action is attempted while stopped."""


class DesktopRuntimeState:
    """Mutable state for desktop runtime safety controls."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._stopped = False
        self._reason: str | None = None
        self._stopped_at: float | None = None

    def snapshot(self) -> DesktopRuntimeSnapshot:
        with self._lock:
            return DesktopRuntimeSnapshot(
                stopped=self._stopped,
                reason=self._reason,
                stopped_at=self._stopped_at,
            )

    def emergency_stop(self, reason: str | None = None) -> DesktopRuntimeSnapshot:
        with self._lock:
            self._stopped = True
            self._reason = reason or "Emergency stop requested"
            self._stopped_at = time()
            return DesktopRuntimeSnapshot(
                stopped=self._stopped,
                reason=self._reason,
                stopped_at=self._stopped_at,
            )

    def clear_stop(self) -> DesktopRuntimeSnapshot:
        with self._lock:
            self._stopped = False
            self._reason = None
            self._stopped_at = None
            return DesktopRuntimeSnapshot(
                stopped=self._stopped,
                reason=self._reason,
                stopped_at=self._stopped_at,
            )

    def ensure_active(self) -> None:
        snapshot = self.snapshot()
        if snapshot.stopped:
            detail = snapshot.reason or "Emergency stop requested"
            raise DesktopRuntimeStoppedError(f"desktop runtime stopped: {detail}")


_DEFAULT_RUNTIME_STATE = DesktopRuntimeState()


def get_default_desktop_runtime_state() -> DesktopRuntimeState:
    return _DEFAULT_RUNTIME_STATE


__all__ = [
    "DesktopRuntimeSnapshot",
    "DesktopRuntimeState",
    "DesktopRuntimeStoppedError",
    "get_default_desktop_runtime_state",
]
