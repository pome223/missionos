"""Desktop client factory helpers."""

from __future__ import annotations

import os
import platform

from src.desktop.client import DesktopClient
from src.desktop.fake_client import FakeDesktopClient
from src.desktop.pyobjc_client import PyObjCDesktopClient
from src.desktop.runtime import get_default_desktop_runtime_state


def build_default_desktop_client() -> DesktopClient:
    runtime_state = get_default_desktop_runtime_state()
    mode = os.getenv("BOILED_CLAW_DESKTOP_CLIENT", "auto").strip().lower()
    if mode == "fake":
        return FakeDesktopClient(runtime_state=runtime_state)

    if platform.system() != "Darwin":
        return FakeDesktopClient(runtime_state=runtime_state)

    appkit_module = None
    quartz_module = None

    try:
        import AppKit  # type: ignore

        appkit_module = AppKit
    except Exception:
        if mode == "pyobjc":
            return FakeDesktopClient(runtime_state=runtime_state)

    try:
        import ApplicationServices  # type: ignore

        quartz_module = ApplicationServices
    except Exception:
        try:
            import Quartz  # type: ignore

            quartz_module = Quartz
        except Exception:
            if mode == "pyobjc":
                return FakeDesktopClient(runtime_state=runtime_state)

    return PyObjCDesktopClient(
        appkit_module=appkit_module,
        quartz_module=quartz_module,
        runtime_state=runtime_state,
    )


__all__ = ["build_default_desktop_client"]
