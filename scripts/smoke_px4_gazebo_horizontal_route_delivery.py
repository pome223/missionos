#!/usr/bin/env python3
"""Compatibility alias for the formal PX4/Gazebo route runtime.

Existing imports receive the runtime module itself so callers that monkeypatch
legacy globals keep the same behavior.  New production code should invoke
``python -m src.runtime.px4_gazebo_route.entrypoint`` directly.
"""

from __future__ import annotations

import sys

from src.runtime.px4_gazebo_route import entrypoint as _entrypoint


if __name__ == "__main__":
    raise SystemExit(_entrypoint.main())


sys.modules[__name__] = _entrypoint
