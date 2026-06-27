"""Network hardening helpers for local bridge services."""

from __future__ import annotations

import ipaddress
import socket


def is_loopback_host(host: str) -> bool:
    normalized = (host or "").strip()
    if not normalized:
        return False

    lowered = normalized.lower()
    if lowered in {"localhost", "127.0.0.1", "::1"}:
        return True

    candidate = normalized.strip("[]").split("%", 1)[0]
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(normalized, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False

    resolved = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        address = str(sockaddr[0]).split("%", 1)[0]
        try:
            resolved.append(ipaddress.ip_address(address).is_loopback)
        except ValueError:
            return False
    return bool(resolved) and all(resolved)


def enforce_loopback_bind(
    host: str,
    *,
    service_name: str,
    allow_remote_bind: bool = False,
) -> None:
    if host == "stdio" or allow_remote_bind:
        return
    if not is_loopback_host(host):
        raise ValueError(
            f"{service_name} only supports loopback bind addresses by default. "
            "Set BRIDGE_ALLOW_REMOTE_BIND=true to override."
        )
