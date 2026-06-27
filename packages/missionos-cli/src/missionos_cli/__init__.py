"""MissionOS operator CLI.

The operator-facing command surface for MissionOS. This package is a
self-contained HTTP client: every command talks to a MissionOS Gateway over
HTTP and renders the result. It carries no backend logic of its own.

``cli.py`` is a byte-identical copy of the operator CLI shipped in the
research mothership, so behaviour is the same by construction.
"""

from missionos_cli.cli import missionos

__all__ = ["missionos"]
