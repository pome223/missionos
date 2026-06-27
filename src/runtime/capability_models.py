"""Shared runtime capability models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional


RuntimeInvoker = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class RuntimeCapabilitySpec:
    name: str
    provider: str
    description: str
    risk: str
    requires_approval: bool
    transport: str
    bridge_capability: Optional[str]
    invoker: RuntimeInvoker


__all__ = ["RuntimeCapabilitySpec", "RuntimeInvoker"]
