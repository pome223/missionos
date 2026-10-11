"""Single loopback HTTP exchange with a wall deadline and no retries or redirects."""

from __future__ import annotations

from http.client import HTTPConnection
import json
import math
import socket
import threading
import time

MAX_BYTES = 24_000_000


def exchange(port, path, payload=None, timeout=75):
    """Use only the two native loopback routes; return JSON or fail closed.

    Socket timeouts alone bound inactivity, not a continuously trickling reply.
    The watchdog shuts down the retained socket, including when HTTP/1.0 has
    detached it from HTTPConnection while a response file still owns the fd.
    This ends local transport only; it does not stop externally owned inference.
    """
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Shadow HTTP requires an explicit loopback port")
    if path not in ("health", "infer") or (path == "health" and payload is not None):
        raise ValueError("Shadow HTTP route or payload is not allowed")
    if type(timeout) not in (int, float) or not 0 < timeout <= 75 or not math.isfinite(timeout):
        raise ValueError("Shadow HTTP requires a finite deadline of at most 75 seconds")
    deadline = time.monotonic() + timeout
    data = json.dumps(payload, allow_nan=False).encode() if payload is not None else None
    if data is not None and len(data) > MAX_BYTES:
        raise ValueError("Oversized shadow HTTP request")
    expired = threading.Event()
    guard = threading.Lock()
    retained_socket = None

    def remaining():
        value = deadline - time.monotonic()
        if expired.is_set() or value <= 0:
            raise TimeoutError("Shadow HTTP wall deadline exceeded")
        return value

    def interrupt():
        expired.set()
        with guard:
            if retained_socket is not None:
                try:
                    retained_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    connection = HTTPConnection("127.0.0.1", port, timeout=remaining())
    response = None
    watchdog = threading.Timer(remaining(), interrupt)
    watchdog.name = "yokohama-shadow-http-deadline"
    watchdog.daemon = True
    watchdog.start()
    try:
        connection.connect()
        with guard:
            retained_socket = connection.sock
            # The watchdog may already have fired while connect was pending.
            retained_socket.settimeout(remaining())
        connection.request(
            "POST" if data is not None else "GET", "/" + path, body=data,
            headers={"Content-Type": "application/json", "Connection": "close"},
        )
        remaining()
        response = connection.getresponse()
        remaining()
        if response.status != 200:
            raise ValueError(f"Shadow HTTP status {response.status}")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdecimal() or int(length) > MAX_BYTES):
            raise ValueError("Oversized or invalid shadow HTTP response length")
        raw = response.read(MAX_BYTES + 1)
        remaining()
        if len(raw) > MAX_BYTES:
            raise ValueError("Oversized shadow HTTP response")
        if length is not None and len(raw) != int(length):
            raise ValueError("Incomplete shadow HTTP response")
        value = json.loads(raw)
        remaining()
        return value
    except Exception as exc:
        if expired.is_set() or time.monotonic() >= deadline or isinstance(exc, TimeoutError):
            raise TimeoutError("Shadow HTTP wall deadline exceeded") from exc
        raise
    finally:
        watchdog.cancel()
        watchdog.join()
        if response is not None:
            response.close()
        connection.close()
