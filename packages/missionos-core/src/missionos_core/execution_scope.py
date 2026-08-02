"""Backend-neutral execution-scope vocabulary."""

from __future__ import annotations

from enum import Enum


class HardwareExecutionMode(str, Enum):
    """Exact scope in which runtime evidence was observed.

    Values are labels, not an ordering. Evidence never transfers between them.
    """

    SCHEMA_EXAMPLE_ONLY = "schema_example_only"
    LOOPBACK = "loopback"
    SIM = "sim"
    HITL = "hitl"
    BENCH = "bench"
    CAGE = "cage"
    FIELD = "field"


def parse_hardware_execution_mode(
    value: object,
) -> HardwareExecutionMode | None:
    """Parse the closed vocabulary without inventing a default."""

    try:
        return HardwareExecutionMode(value)
    except (TypeError, ValueError):
        return None


__all__ = ["HardwareExecutionMode", "parse_hardware_execution_mode"]
