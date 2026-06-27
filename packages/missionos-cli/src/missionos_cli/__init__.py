"""MissionOS operator CLI.

The operator-facing command surface for MissionOS. The primary public path is
the LLM-in-the-loop `missionos chat` surface. Most mission commands talk to a
MissionOS Gateway over HTTP, while local labs and opt-in SITL helpers may call
runtime modules directly.
"""

from missionos_cli.cli import missionos

__all__ = ["missionos"]
