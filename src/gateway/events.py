"""
Typed WebSocket event definitions.

This module re-exports from protocol.py for backward compatibility.
All new event builders are defined in protocol.py.

Server -> Client:
  connected       : connection established (with protocol version)
  chat.token      : streaming token
  chat.done       : agent response complete / abort confirmation
  chat.history    : transcript history response
  tool.start      : tool invocation started
  tool.result     : tool invocation finished
  system.event    : subagent / cron notification
  health.tick     : heartbeat (30s)
  cron.update     : cron job state change
  tools.approval_request : request user approval for tool execution
  control.approval_request : request human approval for a control-loop plan

Client -> Server:
  chat.send       : send message
  chat.inject     : inject system/context message
  chat.abort      : cancel running agent
  chat.history    : request transcript history
  presence.ping   : keepalive
  tools.approval  : respond to approval request
"""

# Re-export all event builders from protocol for backward compatibility
from src.gateway.protocol import (
    PROTOCOL_VERSION,
    ev_connected,
    ev_chat_done,
    ev_chat_token,
    ev_chat_history,
    ev_tool_start,
    ev_tool_result,
    ev_system_event,
    ev_health_tick,
    ev_cron_update,
    ev_tools_approval_request,
    ev_control_approval_request,
    normalize_client_event,
    validate_client_event,
    get_schema,
    make_request_id,
    EVENT_SCHEMAS,
)

__all__ = [
    "PROTOCOL_VERSION",
    "ev_connected",
    "ev_chat_done",
    "ev_chat_token",
    "ev_chat_history",
    "ev_tool_start",
    "ev_tool_result",
    "ev_system_event",
    "ev_health_tick",
    "ev_cron_update",
    "ev_tools_approval_request",
    "ev_control_approval_request",
    "normalize_client_event",
    "validate_client_event",
    "get_schema",
    "make_request_id",
    "EVENT_SCHEMAS",
]
