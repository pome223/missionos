"""Gateway package with lazy compatibility exports."""

from __future__ import annotations

from typing import Any

__all__ = [
    "GatewayServer",
    "create_gateway",
    "SessionManager",
    "MessageRouter",
    "Message",
    "MessageType",
]


def __getattr__(name: str) -> Any:
    if name in {"GatewayServer", "create_gateway"}:
        from src.gateway.server import GatewayServer, create_gateway

        mapping = {
            "GatewayServer": GatewayServer,
            "create_gateway": create_gateway,
        }
        return mapping[name]

    if name == "SessionManager":
        from src.gateway.session_manager import SessionManager

        return SessionManager

    if name in {"MessageRouter", "Message", "MessageType"}:
        from src.gateway.router import Message, MessageRouter, MessageType

        mapping = {
            "MessageRouter": MessageRouter,
            "Message": Message,
            "MessageType": MessageType,
        }
        return mapping[name]

    raise AttributeError(f"module 'src.gateway' has no attribute {name!r}")
