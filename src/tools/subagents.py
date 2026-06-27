"""
Sub-agent orchestration tools.

Google ADK の AgentTool / sub_agents 構成を補完するための
sessions_spawn / subagents_list / subagents_steer / subagents_kill を提供する。

動的エージェント生成:
  sessions_spawn_dynamic — 実行時にシステムプロンプトと MCP サーバーを指定して
  Agent を生成しバックグラウンド実行する。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from google.adk.agents import Agent
from google.adk.agents.context import Context as ToolContext
from google.adk.runners import Runner
from google.genai import types

from src.agents.sub_agents import SUB_AGENTS
from src.agents.model_config import DEFAULT_MODEL, resolve_agent_model
from src.config.settings import get_settings
from src.memory_lifecycle.adk_memory_service import get_promoted_memory_service
from src.runtime.session_service import create_session_service
from src.security.audit import AuditEventType, get_audit_logger
from src.security.tool_policy import get_tool_policy_engine
from src.tools.tasks import create_task_record, update_task_record

_AGENT_MAP = {agent.name: agent for agent in SUB_AGENTS}

_DEFAULT_DYNAMIC_MODEL = DEFAULT_MODEL.name


def _build_mcp_toolsets(mcp_servers: list[dict]) -> list:
    """MCP サーバー設定リストから McpToolset のリストを生成する。

    各エントリは以下のフォーマット:
      {"type": "sse",   "url": "http://..."}
      {"type": "http",  "url": "http://..."}
      {"type": "stdio", "command": "npx", "args": [...], "env": {...}}
    """
    from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
    from google.adk.tools.mcp_tool.mcp_session_manager import (
        SseConnectionParams,
        StdioServerParameters,
        StreamableHTTPConnectionParams,
    )

    toolsets: list = []
    for i, server in enumerate(mcp_servers):
        t = (server.get("type") or "sse").lower()
        if t == "sse":
            if not server.get("url"):
                raise ValueError(f"mcp_servers[{i}]: 'url' is required for type 'sse'")
            toolsets.append(McpToolset(
                connection_params=SseConnectionParams(url=server["url"])
            ))
        elif t == "http":
            if not server.get("url"):
                raise ValueError(f"mcp_servers[{i}]: 'url' is required for type 'http'")
            toolsets.append(McpToolset(
                connection_params=StreamableHTTPConnectionParams(url=server["url"])
            ))
        elif t == "stdio":
            if not server.get("command"):
                raise ValueError(f"mcp_servers[{i}]: 'command' is required for type 'stdio'")
            toolsets.append(McpToolset(
                connection_params=StdioServerParameters(
                    command=server["command"],
                    args=server.get("args") or [],
                    env=server.get("env") or None,
                )
            ))
        else:
            raise ValueError(
                f"mcp_servers[{i}]: unknown type {t!r}. Use 'sse', 'http', or 'stdio'."
            )
    return toolsets


@dataclass
class SubagentRunState:
    run_id: str
    task_id: Optional[str]
    agent_name: str
    mode: str
    requester_session_id: str
    user_id: str
    app_name: str
    created_at: float = field(default_factory=time.time)
    status: str = "accepted"
    current_task: Optional[str] = None
    last_result: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    session_id: Optional[str] = None
    messages_processed: int = 0
    parent_run_id: Optional[str] = None
    spawn_depth: int = 1  # root agent から何段目か (root=0, 直接 spawn=1, ...)
    dynamic_instruction: Optional[str] = None  # 動的生成エージェントのシステムプロンプト
    propagated_approvals: int = 0
    queue: asyncio.Queue[Optional[str]] = field(default_factory=asyncio.Queue, repr=False)
    worker: Optional[asyncio.Task] = field(default=None, repr=False)

    def to_view(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "agent_name": self.agent_name,
            "mode": self.mode,
            "status": self.status,
            "requester_session_id": self.requester_session_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "parent_run_id": self.parent_run_id,
            "spawn_depth": self.spawn_depth,
            "dynamic": self.dynamic_instruction is not None,
            "propagated_approvals": self.propagated_approvals,
            "current_task": self.current_task,
            "messages_processed": self.messages_processed,
            "pending_messages": self.queue.qsize(),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "last_result": self.last_result,
            "error": self.error,
        }


class SubagentManager:
    """In-process background sub-agent run manager."""

    def __init__(self):
        self._runs: Dict[str, SubagentRunState] = {}
        self._lock = asyncio.Lock()
        self._notifier: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None
        self._audit_logger = get_audit_logger()

    def set_notifier(self, notifier: Optional[Callable[[Dict[str, Any]], Awaitable[None]]]) -> None:
        self._notifier = notifier

    @staticmethod
    def _task_title(agent_name: str, task: str) -> str:
        preview = " ".join(task.split()).strip()
        if len(preview) > 72:
            preview = preview[:69].rstrip() + "..."
        return f"Subagent {agent_name}: {preview or 'task'}"

    def _sync_task(
        self,
        state: SubagentRunState,
        *,
        status: Optional[str] = None,
        artifacts: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
        approval_dependencies: Optional[list[str]] = None,
    ) -> None:
        if not state.task_id:
            return
        update_task_record(
            state.task_id,
            status=status or state.status,
            artifacts=artifacts,
            metadata=metadata,
            error=error,
            run_id=state.run_id,
            approval_dependencies=approval_dependencies,
        )

    async def spawn(
        self,
        *,
        task: str,
        agent_name: str,
        requester_session_id: str,
        user_id: str,
        app_name: str,
        mode: str = "run",
        run_timeout_seconds: int = 0,
        _agent: Optional[Agent] = None,  # 動的生成エージェント（内部使用）
        _dynamic_instruction: Optional[str] = None,
    ) -> Dict[str, Any]:
        if _agent is None and agent_name not in _AGENT_MAP:
            return {
                "status": "error",
                "error": f"Unknown agent: {agent_name}",
                "available_agents": sorted(_AGENT_MAP.keys()),
            }

        normalized_mode = (mode or "run").strip().lower()
        if normalized_mode not in {"run", "session"}:
            return {"status": "error", "error": 'mode must be "run" or "session"'}

        run_timeout = max(0, int(run_timeout_seconds or 0))
        settings = get_settings()

        prefix = "dyn" if _agent is not None else "sub"
        run_id = f"{prefix}_{uuid.uuid4().hex[:12]}"

        async with self._lock:
            # 並行数チェック
            _active_statuses = {"accepted", "running", "idle"}
            active_all = [r for r in self._runs.values() if r.status in _active_statuses]
            if len(active_all) >= settings.subagent_max_concurrent:
                return {
                    "status": "error",
                    "error": (
                        f"Max concurrent subagents ({settings.subagent_max_concurrent}) reached. "
                        "Kill or wait for existing runs to complete."
                    ),
                    "active_count": len(active_all),
                }
            active_session = [r for r in active_all if r.requester_session_id == requester_session_id]
            if len(active_session) >= settings.subagent_max_per_session:
                return {
                    "status": "error",
                    "error": (
                        f"Max subagents per session ({settings.subagent_max_per_session}) reached. "
                        "Kill or wait for existing runs to complete."
                    ),
                    "session_active_count": len(active_session),
                }

            # 親ラン検出: requester_session_id が既存サブエージェントの session_id と一致する場合
            parent_run_id: Optional[str] = None
            parent_depth: int = 0
            for existing in self._runs.values():
                if existing.session_id == requester_session_id and existing.status in _active_statuses:
                    parent_run_id = existing.run_id
                    parent_depth = existing.spawn_depth
                    break

            # スポーン深度チェック
            child_depth = parent_depth + 1
            if child_depth > settings.subagent_max_spawn_depth:
                return {
                    "status": "error",
                    "error": (
                        f"Spawn depth limit ({settings.subagent_max_spawn_depth}) exceeded. "
                        f"Current depth would be {child_depth}. "
                        "Increase SUBAGENT_MAX_SPAWN_DEPTH to allow deeper nesting."
                    ),
                    "current_depth": child_depth,
                    "max_depth": settings.subagent_max_spawn_depth,
                }

            state = SubagentRunState(
                run_id=run_id,
                task_id=None,
                agent_name=agent_name,
                mode=normalized_mode,
                requester_session_id=requester_session_id,
                user_id=user_id,
                app_name=app_name,
                current_task=task,
                parent_run_id=parent_run_id,
                spawn_depth=child_depth,
                dynamic_instruction=_dynamic_instruction,
            )
            task_record = create_task_record(
                kind="subagent",
                title=self._task_title(agent_name, task),
                status="accepted",
                owner_session_id=requester_session_id,
                owner_user_id=user_id,
                run_id=run_id,
                approval_dependencies=[],
                artifacts={
                    "subagent": {
                        "agent_name": agent_name,
                        "mode": normalized_mode,
                        "current_task": task,
                        "parent_run_id": parent_run_id,
                        "spawn_depth": child_depth,
                    }
                },
                metadata={
                    "dynamic": _dynamic_instruction is not None,
                    "dynamic_instruction": _dynamic_instruction,
                },
            )
            state.task_id = str(task_record["task_id"])
            self._runs[run_id] = state

        await state.queue.put(task)
        state.worker = asyncio.create_task(
            self._worker_loop(state, run_timeout, _agent),
            name=f"subagent:{run_id}",
        )

        self._audit_logger.log(
            event_type=AuditEventType.AGENT_MESSAGE,
            user_id=user_id,
            session_id=requester_session_id,
            action="subagent_spawn",
            resource=agent_name,
            result="accepted",
            metadata={"run_id": run_id, "task_id": state.task_id, "mode": normalized_mode},
        )

        return {
            "status": "accepted",
            "run_id": run_id,
            "task_id": state.task_id,
            "agent_name": agent_name,
            "mode": normalized_mode,
            "requester_session_id": requester_session_id,
        }

    async def list_runs(self, *, requester_session_id: Optional[str] = None) -> Dict[str, Any]:
        async with self._lock:
            runs = list(self._runs.values())
        if requester_session_id:
            runs = [r for r in runs if r.requester_session_id == requester_session_id]
        runs.sort(key=lambda r: r.created_at, reverse=True)
        return {"runs": [r.to_view() for r in runs], "count": len(runs), "success": True}

    async def steer(
        self,
        *,
        run_id: str,
        message: str,
        requester_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        state = await self._get_run(run_id)
        if state is None:
            return {"success": False, "error": f"run not found: {run_id}"}
        if requester_session_id and state.requester_session_id != requester_session_id:
            return {"success": False, "error": "run is not owned by this session"}
        if state.mode != "session":
            return {
                "success": False,
                "error": 'steer requires mode="session". Spawn with mode="session".',
            }
        if state.worker is None or state.worker.done():
            return {"success": False, "error": "run is not active"}

        await state.queue.put(message)
        return {
            "success": True,
            "run_id": state.run_id,
            "task_id": state.task_id,
            "status": state.status,
            "pending_messages": state.queue.qsize(),
        }

    async def kill(
        self,
        *,
        run_id: str,
        requester_session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        state = await self._get_run(run_id)
        if state is None:
            return {"success": False, "error": f"run not found: {run_id}"}
        if requester_session_id and state.requester_session_id != requester_session_id:
            return {"success": False, "error": "run is not owned by this session"}

        worker = state.worker
        if worker is None or worker.done():
            return {"success": False, "run_id": run_id, "error": "run is not active"}

        # 子孫を深さ優先で再帰的に先にキャンセル
        killed_children = await self._cascade_kill(run_id)

        # 自身をキャンセル
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

        state.status = "cancelled"
        state.ended_at = time.time()
        self._sync_task(
            state,
            status="cancelled",
            artifacts={"subagent": {"killed_children": killed_children}},
            error="cancelled",
        )

        self._audit_logger.log(
            event_type=AuditEventType.AGENT_MESSAGE,
            user_id=state.user_id,
            session_id=state.requester_session_id,
            action="subagent_kill",
            resource=state.agent_name,
            result="cancelled",
            metadata={"run_id": run_id, "task_id": state.task_id, "killed_children": killed_children},
        )

        return {
            "success": True,
            "run_id": run_id,
            "task_id": state.task_id,
            "status": state.status,
            "killed_children": killed_children,
        }

    async def _cascade_kill(self, run_id: str) -> List[str]:
        """run_id の全子孫を深さ優先で再帰的にキャンセルする。
        キャンセルした子孫の run_id リストを返す（自身は含まない）。"""
        async with self._lock:
            children = [
                r for r in self._runs.values()
                if r.parent_run_id == run_id
                and r.worker is not None
                and not r.worker.done()
            ]

        killed: List[str] = []
        for child in children:
            # 孫以下を先に kill（深さ優先）
            descendants = await self._cascade_kill(child.run_id)
            killed.extend(descendants)

            # 子自身をキャンセル
            if child.worker and not child.worker.done():
                child.worker.cancel()
                try:
                    await child.worker
                except asyncio.CancelledError:
                    pass
                child.status = "cancelled"
                child.ended_at = time.time()
                self._sync_task(child, status="cancelled", error="cancelled")
                killed.append(child.run_id)

                self._audit_logger.log(
                    event_type=AuditEventType.AGENT_MESSAGE,
                    user_id=child.user_id,
                    session_id=child.requester_session_id,
                    action="subagent_kill_cascade",
                    resource=child.agent_name,
                    result="cancelled",
                    metadata={"run_id": child.run_id, "task_id": child.task_id, "killed_by_parent": run_id},
                )

        return killed

    async def shutdown(self) -> None:
        async with self._lock:
            workers = [state.worker for state in self._runs.values() if state.worker]
        for worker in workers:
            if worker and not worker.done():
                worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    async def _get_run(self, run_id: str) -> Optional[SubagentRunState]:
        async with self._lock:
            return self._runs.get(run_id)

    async def spawn_dynamic(
        self,
        *,
        task: str,
        instruction: str,
        mcp_servers: list[dict],
        model: str = _DEFAULT_DYNAMIC_MODEL,
        requester_session_id: str,
        user_id: str,
        app_name: str,
        mode: str = "run",
        run_timeout_seconds: int = 0,
    ) -> Dict[str, Any]:
        """システムプロンプトと MCP サーバーを指定して動的にエージェントを生成・実行する。"""
        try:
            toolsets = _build_mcp_toolsets(mcp_servers)
        except ValueError as e:
            return {"status": "error", "error": str(e)}

        run_id = f"dyn_{uuid.uuid4().hex[:12]}"
        agent = Agent(
            name=run_id,
            model=resolve_agent_model(model),
            instruction=instruction,
            tools=toolsets,
        )

        return await self.spawn(
            task=task,
            agent_name=run_id,
            requester_session_id=requester_session_id,
            user_id=user_id,
            app_name=app_name,
            mode=mode,
            run_timeout_seconds=run_timeout_seconds,
            _agent=agent,
            _dynamic_instruction=instruction,
        )

    async def _worker_loop(self, state: SubagentRunState, run_timeout_seconds: int, agent: Optional[Agent] = None) -> None:
        resolved_agent = agent if agent is not None else _AGENT_MAP.get(state.agent_name)
        if resolved_agent is None:
            state.status = "failed"
            state.error = f"Agent not found: {state.agent_name}"
            state.ended_at = time.time()
            self._sync_task(state, status="failed", error=state.error)
            return

        session_service = create_session_service()
        memory_service = get_promoted_memory_service()
        runner = Runner(
            agent=resolved_agent,
            app_name=state.app_name,
            session_service=session_service,
            memory_service=memory_service,
        )
        session = await session_service.create_session(
            app_name=state.app_name,
            user_id=state.user_id,
        )
        state.session_id = session.id
        propagated = get_tool_policy_engine().propagate_approvals_to_session(
            source_session_id=state.requester_session_id,
            target_session_id=session.id,
            agent_name=state.agent_name,
        )
        state.propagated_approvals = len(propagated)
        dependency_ids = [
            str(item.get("source_request_id") or item.get("request_id"))
            for item in propagated
            if item.get("source_request_id") or item.get("request_id")
        ]
        self._sync_task(
            state,
            status=state.status,
            artifacts={
                "subagent": {
                    "session_id": session.id,
                    "propagated_approvals": state.propagated_approvals,
                }
            },
            approval_dependencies=dependency_ids,
        )

        while True:
            task_message = await state.queue.get()
            if task_message is None:
                break

            if state.started_at is None:
                state.started_at = time.time()
            state.status = "running"
            state.current_task = task_message
            self._sync_task(
                state,
                status="running",
                artifacts={
                    "subagent": {
                        "current_task": task_message,
                        "messages_processed": state.messages_processed,
                        "session_id": session.id,
                    }
                },
            )

            try:
                result_text = await self._run_agent_turn(
                    runner=runner,
                    user_id=state.user_id,
                    session_id=session.id,
                    message=task_message,
                    run_timeout_seconds=run_timeout_seconds,
                )
                state.messages_processed += 1
                state.last_result = (result_text or "").strip() or "(empty response)"
                state.error = None
                state.ended_at = time.time()
                completion_status = "completed" if state.mode == "run" else "idle"
                self._sync_task(
                    state,
                    status=completion_status,
                    artifacts={
                        "subagent": {
                            "current_task": task_message,
                            "last_result": state.last_result,
                            "messages_processed": state.messages_processed,
                            "session_id": session.id,
                        }
                    },
                    error=None,
                )

                await self._notify(
                    state=state,
                    message=(
                        f"[subagent:{state.agent_name}] completed ({state.run_id})\n"
                        f"{state.last_result}"
                    ),
                    event="completed",
                )

                if state.mode == "run":
                    state.status = "completed"
                    break

                state.status = "idle"
            except asyncio.CancelledError:
                state.status = "cancelled"
                state.ended_at = time.time()
                self._sync_task(state, status="cancelled", error="cancelled")
                raise
            except Exception as exc:
                state.error = str(exc)
                state.status = "failed"
                state.ended_at = time.time()
                self._sync_task(
                    state,
                    status="failed",
                    artifacts={
                        "subagent": {
                            "current_task": task_message,
                            "messages_processed": state.messages_processed,
                            "session_id": session.id,
                        }
                    },
                    error=state.error,
                )
                await self._notify(
                    state=state,
                    message=f"[subagent:{state.agent_name}] failed ({state.run_id}): {state.error}",
                    event="failed",
                )
                break

    async def _run_agent_turn(
        self,
        *,
        runner: Runner,
        user_id: str,
        session_id: str,
        message: str,
        run_timeout_seconds: int,
    ) -> str:
        content = types.Content(role="user", parts=[types.Part(text=message)])
        response_text = ""

        async def _collect() -> str:
            nonlocal response_text
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=content,
            ):
                if event.is_final_response() and event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.text:
                            response_text += part.text
            return response_text

        if run_timeout_seconds > 0:
            return await asyncio.wait_for(_collect(), timeout=run_timeout_seconds)
        return await _collect()

    async def _notify(self, *, state: SubagentRunState, message: str, event: str) -> None:
        self._audit_logger.log(
            event_type=AuditEventType.AGENT_MESSAGE,
            user_id=state.user_id,
            session_id=state.requester_session_id,
            action=f"subagent_{event}",
            resource=state.agent_name,
            result=state.status,
            metadata={"run_id": state.run_id, "task_id": state.task_id, "message_preview": message[:120]},
        )

        if self._notifier is None:
            return
        try:
            await self._notifier(
                {
                    "run_id": state.run_id,
                    "task_id": state.task_id,
                    "agent_name": state.agent_name,
                    "requester_session_id": state.requester_session_id,
                    "event": event,
                    "message": message,
                    "status": state.status,
                }
            )
        except Exception:
            # ノーティファイ失敗は本体処理を止めない
            return


_subagent_manager: Optional[SubagentManager] = None


def get_subagent_manager() -> SubagentManager:
    global _subagent_manager
    if _subagent_manager is None:
        _subagent_manager = SubagentManager()
    return _subagent_manager


def set_subagent_notifier(
    notifier: Optional[Callable[[Dict[str, Any]], Awaitable[None]]],
) -> None:
    get_subagent_manager().set_notifier(notifier)


async def reset_subagent_manager_for_tests() -> None:
    global _subagent_manager
    if _subagent_manager is not None:
        await _subagent_manager.shutdown()
    _subagent_manager = None


def _resolve_context(tool_context: Optional[ToolContext]) -> Dict[str, str]:
    session = getattr(tool_context, "session", None)
    return {
        "user_id": getattr(tool_context, "user_id", None) or "unknown_user",
        "session_id": getattr(session, "id", None) or "unknown_session",
        "app_name": getattr(session, "app_name", None) or "boiled-claw",
    }


async def agents_list() -> Dict[str, Any]:
    """利用可能なサブエージェント一覧を返す。"""
    return {
        "success": True,
        "agents": [
            {"id": agent.name, "description": agent.description or ""}
            for agent in SUB_AGENTS
        ],
    }


async def sessions_spawn_dynamic(
    task: str,
    instruction: str,
    mcp_servers: str = "[]",
    model: str = _DEFAULT_DYNAMIC_MODEL,
    mode: str = "run",
    run_timeout_seconds: int = 0,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """
    システムプロンプトと MCP サーバーを指定してエージェントを動的に生成しバックグラウンド実行する。

    Args:
        task: エージェントに与える最初のタスク
        instruction: エージェントのシステムプロンプト
        mcp_servers: MCP サーバー設定の JSON 配列文字列。
            各要素は以下のいずれか:
            {"type": "sse",   "url": "http://..."}
            {"type": "http",  "url": "http://..."}
            {"type": "stdio", "command": "npx", "args": [...], "env": {...}}
        model: 使用モデル（デフォルト: settings.agent_model）
        mode: "run"（1回実行）/ "session"（継続セッション）
        run_timeout_seconds: タイムアウト秒数（0 = 無制限）
    """
    ctx = _resolve_context(tool_context)

    try:
        servers: list[dict] = json.loads(mcp_servers) if mcp_servers.strip() else []
    except json.JSONDecodeError as e:
        return {"status": "error", "error": f"mcp_servers is not valid JSON: {e}"}

    if not isinstance(servers, list):
        return {"status": "error", "error": "mcp_servers must be a JSON array"}

    manager = get_subagent_manager()
    return await manager.spawn_dynamic(
        task=task,
        instruction=instruction,
        mcp_servers=servers,
        model=model,
        requester_session_id=ctx["session_id"],
        user_id=ctx["user_id"],
        app_name=ctx["app_name"],
        mode=mode,
        run_timeout_seconds=run_timeout_seconds,
    )


async def sessions_spawn(
    task: str,
    agent_id: Optional[str] = None,
    mode: str = "run",
    run_timeout_seconds: int = 0,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """
    サブエージェントをバックグラウンド起動する。

    Args:
        task: 実行タスク
        agent_id: 対象エージェント名（未指定時は web_researcher）
        mode: run (1回実行) / session (継続セッション)
        run_timeout_seconds: 0 なら無制限
    """
    ctx = _resolve_context(tool_context)
    manager = get_subagent_manager()
    return await manager.spawn(
        task=task,
        agent_name=(agent_id or "web_researcher").strip(),
        requester_session_id=ctx["session_id"],
        user_id=ctx["user_id"],
        app_name=ctx["app_name"],
        mode=mode,
        run_timeout_seconds=run_timeout_seconds,
    )


async def subagents_list(tool_context: Optional[ToolContext] = None) -> Dict[str, Any]:
    """現在セッションのサブエージェント実行一覧を返す。"""
    ctx = _resolve_context(tool_context)
    manager = get_subagent_manager()
    return await manager.list_runs(requester_session_id=ctx["session_id"])


async def subagents_steer(
    run_id: str,
    message: str,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """mode=session のサブエージェントに追加入力を送る。"""
    ctx = _resolve_context(tool_context)
    manager = get_subagent_manager()
    return await manager.steer(
        run_id=run_id,
        message=message,
        requester_session_id=ctx["session_id"],
    )


async def subagents_kill(
    run_id: str,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """実行中サブエージェントを停止する。"""
    ctx = _resolve_context(tool_context)
    manager = get_subagent_manager()
    return await manager.kill(
        run_id=run_id,
        requester_session_id=ctx["session_id"],
    )
