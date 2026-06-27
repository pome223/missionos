from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from src.cron.scheduler import get_scheduler
from src.gateway.protocol import (
    ev_chat_done,
    ev_chat_history,
    ev_connected,
    ev_health_tick,
    normalize_client_event,
    validate_client_event,
)
from src.gateway.route_utils import normalize_constraints
from src.security.audit import AuditEventType

if TYPE_CHECKING:
    from src.gateway.server import GatewayServer


def build_websocket_router(server: "GatewayServer") -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws/{user_id}")
    async def websocket_endpoint(
        websocket: WebSocket,
        user_id: str,
        session_id: Optional[str] = Query(default=None),
        token: Optional[str] = Query(default=None),
    ):
        if server.settings.gateway_api_key:
            if token != server.settings.gateway_api_key:
                await websocket.close(code=4401, reason="Unauthorized")
                return
        try:
            user_id = server._resolve_websocket_user_id(
                websocket,
                user_id,
                default_user_id="web_user",
            )
        except HTTPException as exc:
            await websocket.close(code=4401, reason=str(exc.detail))
            return

        session = await server._get_or_create_gateway_session(
            user_id=user_id,
            session_id=session_id,
        )
        session_id = session.id

        await server.manager.connect(websocket, session_id, user_id)

        try:
            # ``connected`` MUST be the first event a client receives on the
            # WS so the typed protocol handshake is deterministic. Audit
            # logging is intentionally deferred until after ``connected`` has
            # been awaited because ``audit_logger.log`` schedules the
            # ``audit.append`` notifier via ``loop.create_task``; if logged
            # first, that task would race ahead of the explicit ``send_json``
            # below and the client would observe ``audit.append`` before
            # ``connected``. See issue #181.
            await server.manager.send_json(session_id, ev_connected(session_id, user_id))
            await server.manager.flush_pending(session_id)
            server.audit_logger.log(
                event_type=AuditEventType.SESSION_START,
                user_id=user_id,
                session_id=session_id,
                action="connect",
                result="success",
            )
            asyncio.create_task(
                get_scheduler().fire_system_event(
                    "connect",
                    {"user_id": user_id, "session_id": session_id},
                ),
                name=f"sys:connect:{session_id}",
            )

            while True:
                raw_data = await websocket.receive_json()
                data = normalize_client_event(raw_data)
                validation_errors = validate_client_event(data)
                if validation_errors:
                    await server._emit_session_event(
                        session_id,
                        source="protocol",
                        status="error",
                        message="; ".join(validation_errors),
                        user_id=user_id,
                    )
                    continue
                event_name = data.get("event", "")

                if event_name == "chat.send":
                    text = (data.get("text") or "").strip()
                    request_id = data.get("request_id")
                    if text:
                        server.transcript.append(
                            session_id,
                            "user",
                            text,
                            user_id=user_id,
                            request_id=request_id,
                        )
                        await server._start_agent_run(session_id, user_id, text, request_id)

                elif event_name == "control.run":
                    goal = (data.get("goal") or "").strip()
                    constraints = normalize_constraints(data.get("constraints"))
                    request_id = data.get("request_id")
                    if goal:
                        server.transcript.append(
                            session_id,
                            "user",
                            goal,
                            user_id=user_id,
                            request_id=request_id,
                            metadata={
                                "type": "control_loop",
                                "constraints": constraints,
                            },
                        )
                        await server._start_control_loop_run(
                            session_id,
                            user_id,
                            goal,
                            constraints,
                            request_id,
                        )

                elif event_name == "chat.inject":
                    text = (data.get("text") or "").strip()
                    role = data.get("role", "system")
                    request_id = data.get("request_id")
                    if text:
                        server.transcript.append(
                            session_id,
                            "inject",
                            text,
                            user_id=user_id,
                            request_id=request_id,
                            metadata={"role": role},
                        )
                        await server._emit_session_event(
                            session_id,
                            source="inject",
                            status="ok",
                            message=f"Injected {role} message into transcript",
                            user_id=user_id,
                        )

                elif event_name == "chat.abort":
                    request_id = data.get("request_id")
                    aborted = await server.manager.abort(session_id)
                    await server._desktop_emergency_stop(
                        session_id=session_id,
                        user_id=user_id,
                        reason="Abort requested from Web UI",
                    )
                    if not aborted:
                        await server.manager.send_json(
                            session_id,
                            ev_chat_done("", request_id, aborted=False),
                        )

                elif event_name == "chat.history":
                    request_id = data.get("request_id")
                    target_session = data.get("session_id") or session_id
                    if not server.transcript.has_session(target_session, user_id):
                        await server._emit_session_event(
                            session_id,
                            source="protocol",
                            status="error",
                            message=f"session not found: {target_session}",
                            user_id=user_id,
                        )
                        continue
                    limit = min(int(data.get("limit") or 100), 500)
                    before = data.get("before")
                    entries = server.transcript.get_history(
                        target_session,
                        limit=limit,
                        before=before,
                    )
                    await server.manager.send_json(
                        session_id,
                        ev_chat_history(
                            [entry.to_dict() for entry in entries],
                            target_session,
                            request_id,
                        ),
                    )

                elif event_name == "presence.ping":
                    await server.manager.send_json(
                        session_id,
                        ev_health_tick(len(server.manager.active_connections)),
                    )

                elif event_name == "tools.approval":
                    await server._resolve_tool_approval_request(
                        request_id=data.get("request_id", ""),
                        approved=bool(data.get("approved", False)),
                        reason=str(data.get("reason") or ""),
                        session_id=session_id,
                        user_id=user_id,
                        source="websocket",
                        scope=data.get("scope"),
                        tool_pattern=data.get("tool_pattern"),
                        path_scope=data.get("path_scope"),
                        expires_at=data.get("expires_at"),
                        propagate_to_subagents=data.get("propagate_to_subagents"),
                    )

        except WebSocketDisconnect:
            pass
        except Exception as exc:
            server.audit_logger.log_error(
                error=str(exc),
                user_id=user_id,
                session_id=session_id,
                context={"endpoint": "websocket"},
            )
        finally:
            server.manager.disconnect(
                session_id,
                preserve_pending=True,
                preserve_user=True,
            )
            server.audit_logger.log(
                event_type=AuditEventType.SESSION_END,
                user_id=user_id,
                session_id=session_id,
                action="disconnect",
                result="success",
            )
            asyncio.create_task(
                get_scheduler().fire_system_event(
                    "disconnect",
                    {"user_id": user_id, "session_id": session_id},
                ),
                name=f"sys:disconnect:{session_id}",
            )

    return router
