"""Shared bridge/runtime-neutral schema models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class BridgePingResult(BaseModel):
    ok: bool = True
    service: str
    version: str
    transport: str


class CapabilityDescriptor(BaseModel):
    name: str
    risk: Literal["low", "medium", "high"]
    requires_approval: bool
    description: str
    implemented: bool = True


class CapabilityListResult(BaseModel):
    capabilities: list[CapabilityDescriptor]


__all__ = [
    "BridgePingResult",
    "CapabilityDescriptor",
    "CapabilityListResult",
]
