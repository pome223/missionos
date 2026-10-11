"""Real loopback transport deadlines, without models or external network access."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import threading
import time

import pytest

from src.runtime import yokohama_shadow_http as transport


@contextmanager
def local_server(mode="json"):
    calls = []
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.0 makes HTTPConnection detach conn.sock after getresponse.
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.do_GET()

        def do_GET(self):
            calls.append((self.command, self.path))
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            try:
                if mode == "headers":
                    self.trickle(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\n{}")
                    return
                if mode in {"body", "chunked"}:
                    self.send_response(200)
                    self.send_header(
                        "Transfer-Encoding" if mode == "chunked" else "Content-Length",
                        "chunked" if mode == "chunked" else "202",
                    )
                    self.end_headers()
                    self.wfile.flush()
                    for _ in range(200):
                        if stop.wait(0.02):
                            break
                        self.wfile.write(b"1\r\n \r\n" if mode == "chunked" else b" ")
                        self.wfile.flush()
                    return
                if mode in {"503", "redirect"}:
                    self.send_response(503 if mode == "503" else 302)
                    if mode == "redirect":
                        self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/infer")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                if mode == "oversized_header":
                    self.send_header("Content-Length", str(transport.MAX_BYTES + 1))
                    self.end_headers()
                    return
                data = (b"x" * (transport.MAX_BYTES + 1) if mode == "oversized_stream"
                        else json.dumps({"method": self.command,
                                         "request": json.loads(body) if body else None}).encode())
                if mode != "oversized_stream":
                    self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def trickle(self, data):
            for byte in data:
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
                if stop.wait(0.02):
                    break

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01},
        name="shadow-http-test-server", daemon=True,
    )
    thread.start()
    try:
        yield server.server_port, calls
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def assert_no_watchdog():
    assert not any(t.name == "yokohama-shadow-http-deadline" for t in threading.enumerate())


@pytest.mark.parametrize("payload", [None, {"request": "explicit test double input"}])
def test_real_loopback_json_ignores_proxy_environment(monkeypatch, payload):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    with local_server() as (port, calls):
        result = transport.exchange(port, "health" if payload is None else "infer", payload)
        assert result == {"method": "GET" if payload is None else "POST", "request": payload}
        assert len(calls) == 1
    assert_no_watchdog()


@pytest.mark.parametrize("mode", ["headers", "body", "chunked"])
def test_wall_deadline_interrupts_continuously_trickled_http(mode):
    with local_server(mode) as (port, calls):
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="wall deadline"):
            transport.exchange(port, "health", timeout=0.25)
        elapsed = time.monotonic() - started
        # The server emits data every 20 ms for >1 s, so an inactivity timeout
        # would not interrupt it. Allow ample scheduler slack on shared CI.
        assert elapsed < 1.5
        assert calls == [("GET", "/health")]
    assert_no_watchdog()


@pytest.mark.parametrize("mode,status", [("503", "503"), ("redirect", "302")])
def test_status_errors_neither_retry_nor_follow_redirect(mode, status):
    with local_server(mode) as (port, calls):
        with pytest.raises(ValueError, match="status " + status):
            transport.exchange(port, "health")
        assert calls == [("GET", "/health")]
    assert_no_watchdog()


def test_24_mb_response_header_limit_rejects_without_reading_body():
    assert transport.MAX_BYTES == 24_000_000
    with local_server("oversized_header") as (port, calls):
        with pytest.raises(ValueError, match="response length"):
            transport.exchange(port, "health")
        assert len(calls) == 1
    assert_no_watchdog()


def test_stream_without_length_cannot_exceed_response_limit(monkeypatch):
    monkeypatch.setattr(transport, "MAX_BYTES", 32)
    with local_server("oversized_stream") as (port, calls):
        with pytest.raises(ValueError, match="Oversized shadow HTTP response"):
            transport.exchange(port, "health")
        assert len(calls) == 1
    assert_no_watchdog()


@pytest.mark.parametrize("port,path,timeout", [
    (True, "health", 1), (80, "health", 1), (65536, "health", 1),
    ("example.invalid", "health", 1), (18117, "../health", 1),
    (18117, "http://example.invalid/", 1), (18117, "health", 0),
    (18117, "health", -1), (18117, "health", 76),
    (18117, "health", float("nan")), (18117, "health", float("inf")),
])
def test_invalid_destination_or_deadline_rejected_before_socket(monkeypatch, port, path, timeout):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid parameters reached HTTPConnection")

    monkeypatch.setattr(transport, "HTTPConnection", forbidden)
    with pytest.raises(ValueError):
        transport.exchange(port, path, timeout=timeout)


def test_request_limit_is_checked_before_connection(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Oversized payload reached HTTPConnection")

    monkeypatch.setattr(transport, "HTTPConnection", forbidden)
    monkeypatch.setattr(transport, "MAX_BYTES", 16)
    with pytest.raises(ValueError, match="Oversized shadow HTTP request"):
        transport.exchange(18117, "infer", {"payload": "x" * 32})
