"""
ファイル操作ツール
"""

import uuid
from pathlib import Path
from typing import Optional

from google.adk.agents.context import Context as ToolContext

from src.bridges.host_bridge_client import get_host_bridge_client
from src.bridges.host_bridge_exec import execute_host_bridge_call
from src.bridges.host_bridge_schema import HostFileReadRequest, HostFileWriteRequest
from src.config.settings import get_settings
from src.security.audit import get_audit_logger
from src.security.policy import get_security_policy
from src.security.tool_policy import get_tool_policy_engine
from src.tools.context import resolve_tool_context


async def _check_write_policy(
    path: str,
    content: str,
    tool_context: Optional[ToolContext],
) -> tuple[Optional[str], Optional[str]]:
    if tool_context is None:
        return None, None

    ctx = resolve_tool_context(tool_context)
    engine = get_tool_policy_engine()
    action, reason = engine.evaluate(ctx["agent_name"], "write_file")
    if action == "allow":
        return None, None
    if action == "deny":
        return f"Tool blocked by policy: {reason}", None

    approved, response_reason, approval_token = await engine.request_approval_with_id(
        tool_name="write_file",
        agent_name=ctx["agent_name"],
        args={"path": str(Path(path).expanduser()), "size": len(content)},
        session_id=ctx["session_id"],
        reason=reason,
    )
    if approved:
        return None, approval_token
    detail = response_reason or reason or "user rejected"
    return f"Tool approval denied: {detail}", approval_token


async def read_file(
    path: str,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """
    ファイルを読み込む

    Args:
        path: 読み込むファイルのパス

    Returns:
        ファイルの内容
    """
    audit_logger = get_audit_logger()
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    policy = get_security_policy()
    allowed, reason = policy.is_path_allowed(path, "read")
    if not allowed:
        audit_logger.log_file_operation(
            "read",
            path,
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            result=f"blocked:{reason}",
        )
        return {"error": f"Access denied: {reason}"}

    settings = get_settings()
    if settings.host_bridge_enabled:
        request = HostFileReadRequest(
            request_id=f"host-file-read-{uuid.uuid4().hex[:12]}",
            session_id=ctx.get("session_id") or "standalone-session",
            user_id=ctx.get("user_id") or "standalone-user",
            agent_name=ctx.get("agent_name") or "unknown_agent",
            approval_token=None,
            path=path,
        )
        result, payload = await execute_host_bridge_call(
            request=request,
            tool_name="host.file.read",
            args={"path": request.path},
            get_client=get_host_bridge_client,
            invoke=lambda client, req: client.read_file(req),
            ok_getter=lambda result: result.ok,
            error_payload=lambda error: {"error": error},
            metadata={"executor": "host_bridge"},
        )
        if result is not None:
            audit_logger.log_file_operation(
                "read",
                result.path or path,
                user_id=ctx.get("user_id") or None,
                session_id=ctx.get("session_id") or None,
                result="bridge_success" if result.ok else f"bridge_error:{result.error}",
                metadata={"executor": "host_bridge", "request_id": request.request_id},
            )
            return {
                "path": result.path,
                "content": result.content,
                "size": result.size,
            }
        audit_logger.log_file_operation(
            "read",
            path,
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            result=f"bridge_error:{payload['error']}",
            metadata={"executor": "host_bridge", "request_id": request.request_id},
        )
        return payload

    try:
        file_path = Path(path).expanduser().resolve()
        content = file_path.read_text(encoding="utf-8")
        audit_logger.log_file_operation(
            "read",
            str(file_path),
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            result="success",
        )
        return {
            "path": str(file_path),
            "content": content,
            "size": len(content),
        }
    except FileNotFoundError:
        audit_logger.log_file_operation(
            "read",
            path,
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            result="not_found",
        )
        return {"error": f"File not found: {path}"}
    except PermissionError:
        audit_logger.log_file_operation(
            "read",
            path,
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            result="permission_denied",
        )
        return {"error": f"Permission denied: {path}"}
    except Exception as e:
        audit_logger.log_file_operation(
            "read",
            path,
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            result=f"error:{e}",
        )
        return {"error": str(e)}


async def write_file(
    path: str,
    content: str,
    tool_context: Optional[ToolContext] = None,
) -> dict:
    """
    ファイルに書き込む

    Args:
        path: 書き込むファイルのパス
        content: 書き込む内容

    Returns:
        書き込み結果
    """
    audit_logger = get_audit_logger()
    ctx = resolve_tool_context(tool_context) if tool_context is not None else {}
    policy = get_security_policy()
    allowed, reason = policy.is_path_allowed(path, "write")
    if not allowed:
        audit_logger.log_file_operation(
            "write",
            path,
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            result=f"blocked:{reason}",
        )
        return {"error": f"Access denied: {reason}"}
    content_allowed, content_reason = policy.validate_file_content(content, path)
    if not content_allowed:
        audit_logger.log_file_operation(
            "write",
            path,
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            result=f"blocked:{content_reason}",
        )
        return {"error": f"Content blocked by security policy: {content_reason}"}

    approval_error, approval_token = await _check_write_policy(path, content, tool_context)
    if approval_error:
        audit_logger.log_file_operation(
            "write",
            path,
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            result=approval_error,
        )
        return {"error": approval_error}

    settings = get_settings()
    if settings.host_bridge_enabled:
        request = HostFileWriteRequest(
            request_id=f"host-file-write-{uuid.uuid4().hex[:12]}",
            session_id=ctx.get("session_id") or "standalone-session",
            user_id=ctx.get("user_id") or "standalone-user",
            agent_name=ctx.get("agent_name") or "unknown_agent",
            approval_token=approval_token,
            path=path,
            content=content,
        )
        result, payload = await execute_host_bridge_call(
            request=request,
            tool_name="host.file.write",
            args={"path": request.path, "size": len(request.content)},
            get_client=get_host_bridge_client,
            invoke=lambda client, req: client.write_file(req),
            ok_getter=lambda result: result.ok,
            error_payload=lambda error: {"error": error},
            metadata={"executor": "host_bridge"},
        )
        if result is not None:
            audit_logger.log_file_operation(
                "write",
                result.path or path,
                user_id=ctx.get("user_id") or None,
                session_id=ctx.get("session_id") or None,
                result="bridge_success" if result.ok else f"bridge_error:{result.error}",
                metadata={
                    "executor": "host_bridge",
                    "request_id": request.request_id,
                    "approval_token": approval_token,
                },
            )
            return {
                "path": result.path,
                "size": result.size,
                "success": result.ok,
            }
        audit_logger.log_file_operation(
            "write",
            path,
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            result=f"bridge_error:{payload['error']}",
            metadata={
                "executor": "host_bridge",
                "request_id": request.request_id,
                "approval_token": approval_token,
            },
        )
        return payload

    try:
        file_path = Path(path).expanduser().resolve()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        audit_logger.log_file_operation(
            "write",
            str(file_path),
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            result="success",
        )
        return {
            "path": str(file_path),
            "size": len(content),
            "success": True,
        }
    except PermissionError:
        audit_logger.log_file_operation(
            "write",
            path,
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            result="permission_denied",
        )
        return {"error": f"Permission denied: {path}"}
    except Exception as e:
        audit_logger.log_file_operation(
            "write",
            path,
            user_id=ctx.get("user_id") or None,
            session_id=ctx.get("session_id") or None,
            result=f"error:{e}",
        )
        return {"error": str(e)}
