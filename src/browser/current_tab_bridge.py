"""Chrome extension relay for current-tab browser control."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import websockets

from src.config.settings import get_settings
from src.security.network import enforce_loopback_bind

logger = logging.getLogger(__name__)


class CurrentTabBridgeError(RuntimeError):
    """Raised when the current-tab extension bridge is unavailable or fails."""


HELLO_TIMEOUT_SECONDS = 5.0
ALLOWED_EXTENSION_ORIGIN_PREFIX = "chrome-extension://"


def _origin_is_allowed(origin: str | None) -> bool:
    normalized = str(origin or "").strip().lower()
    return normalized.startswith(ALLOWED_EXTENSION_ORIGIN_PREFIX)


def _extract_origin(websocket: Any) -> str:
    request = getattr(websocket, "request", None)
    headers = getattr(request, "headers", None)
    if headers is not None:
        return str(headers.get("Origin") or "")
    request_headers = getattr(websocket, "request_headers", None)
    if request_headers is not None:
        return str(request_headers.get("Origin") or "")
    return ""


def _extract_request_token(websocket: Any) -> str:
    request = getattr(websocket, "request", None)
    path = str(getattr(request, "path", "") or getattr(websocket, "path", "") or "")
    if not path:
        return ""
    query = urlparse(path).query
    values = parse_qs(query).get("token") or []
    return str(values[0]).strip() if values else ""


def _normalize_current_tab_bridge_host(host: str) -> str:
    normalized = str(host or "").strip().lower()
    if normalized == "host.docker.internal":
        return "127.0.0.1"
    return str(host or "").strip()


class CurrentTabExtensionBridge:
    """WebSocket relay that accepts a single Chrome extension connection."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        shared_token: str | None = None,
        allow_remote_bind: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.shared_token = str(shared_token or "").strip()
        self.allow_remote_bind = allow_remote_bind
        self._server: Any = None
        self._server_lock = asyncio.Lock()
        self._client_lock = asyncio.Lock()
        self._client: Any = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    @property
    def connected(self) -> bool:
        client = self._client
        return client is not None and not getattr(client, "closed", False)

    async def ensure_started(self) -> None:
        async with self._server_lock:
            if self._server is not None:
                return
            enforce_loopback_bind(
                self.host,
                service_name="Current Tab relay",
                allow_remote_bind=self.allow_remote_bind,
            )
            self._server = await websockets.serve(
                self._handle_connection,
                self.host,
                self.port,
                ping_interval=20,
                ping_timeout=20,
            )

    async def _handle_connection(self, websocket: Any) -> None:
        try:
            await self._authenticate_connection(websocket)
        except CurrentTabBridgeError as exc:
            logger.warning("Current Tab auth failed: %s", exc)
            await websocket.close(code=1008, reason=str(exc))
            return

        await websocket.send(json.dumps({"type": "hello_ack"}))
        logger.info("Current Tab extension connected")

        async with self._client_lock:
            previous = self._client
            self._client = websocket

        if previous is not None and not getattr(previous, "closed", False):
            await previous.close()

        try:
            async for raw_message in websocket:
                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError:
                    continue
                msg_type = str(message.get("type") or "").strip().lower()
                if msg_type == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))
                    continue
                await self._handle_message(message)
        finally:
            close_code = getattr(websocket, "close_code", None)
            close_reason = getattr(websocket, "close_reason", None) or "(none)"
            logger.info(
                "Current Tab extension disconnected: code=%s reason=%s",
                close_code,
                close_reason,
            )
            async with self._client_lock:
                if self._client is websocket:
                    self._client = None
            self._fail_pending("Current Tab extension disconnected")

    async def _authenticate_connection(self, websocket: Any) -> None:
        origin = _extract_origin(websocket)
        if not _origin_is_allowed(origin):
            raise CurrentTabBridgeError("Current Tab connection rejected: origin not allowed")

        try:
            raw_hello = await asyncio.wait_for(websocket.recv(), timeout=HELLO_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise CurrentTabBridgeError("Current Tab connection rejected: hello timed out") from exc

        try:
            hello = json.loads(raw_hello)
        except json.JSONDecodeError as exc:
            raise CurrentTabBridgeError("Current Tab connection rejected: invalid hello payload") from exc

        if str(hello.get("type") or "").strip().lower() != "hello":
            raise CurrentTabBridgeError("Current Tab connection rejected: hello message required")

        provided_token = str(hello.get("token") or _extract_request_token(websocket) or "").strip()
        if self.shared_token and provided_token != self.shared_token:
            raise CurrentTabBridgeError("Current Tab connection rejected: invalid relay token")

    async def _handle_message(self, message: dict[str, Any]) -> None:
        message_type = str(message.get("type") or "").strip().lower()
        if message_type != "response":
            return

        request_id = str(message.get("request_id") or "").strip()
        if not request_id:
            return

        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return
        future.set_result(message)

    def _fail_pending(self, error: str) -> None:
        for request_id, future in list(self._pending.items()):
            if future.done():
                continue
            future.set_exception(CurrentTabBridgeError(error))
            self._pending.pop(request_id, None)

    async def call(
        self,
        action: str,
        payload: Optional[dict[str, Any]] = None,
        *,
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        await self.ensure_started()

        client = self._client
        if client is None or getattr(client, "closed", False):
            raise CurrentTabBridgeError(
                "Current Tab extension is not connected. Load the unpacked extension in Chrome."
            )

        request_id = f"ctab_{uuid.uuid4().hex[:12]}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future

        request = {
            "type": "request",
            "request_id": request_id,
            "action": action,
            "payload": payload or {},
        }
        try:
            await client.send(json.dumps(request))
            response = await asyncio.wait_for(future, timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise CurrentTabBridgeError(
                f"Current Tab extension timed out while handling '{action}'"
            ) from exc
        finally:
            self._pending.pop(request_id, None)

        if not bool(response.get("ok")):
            error = str(response.get("error") or "").strip() or f"Current Tab action failed: {action}"
            raise CurrentTabBridgeError(error)

        result = response.get("result")
        return result if isinstance(result, dict) else {}


_current_tab_bridge: Optional[CurrentTabExtensionBridge] = None


def current_tab_bridge_enabled() -> bool:
    return bool(get_settings().current_tab_bridge_enabled)


def get_current_tab_extension_bridge() -> CurrentTabExtensionBridge:
    global _current_tab_bridge

    settings = get_settings()
    if _current_tab_bridge is None:
        _current_tab_bridge = CurrentTabExtensionBridge(
            host=_normalize_current_tab_bridge_host(settings.current_tab_bridge_host),
            port=settings.current_tab_bridge_port,
            shared_token=settings.current_tab_bridge_token,
            allow_remote_bind=settings.bridge_allow_remote_bind,
        )
    return _current_tab_bridge
