"""
Tool-level security with per-agent policies and stateful approvals.

Policy evaluation order:
  1. Check agent-specific rules (if any)
  2. Check default rules
  3. Apply fallback action (deny by default)

Actions:
  allow   - tool execution permitted
  deny    - tool execution blocked
  approve - requires user approval before execution
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
from pathlib import Path
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Literal, Mapping, Optional, Tuple

Action = Literal["allow", "deny", "approve"]
ApprovalState = Literal["pending", "expiring", "approved", "denied", "propagated", "expired"]
ApprovalScope = Literal["single", "session"]

_DEFAULT_APPROVAL_TTL_SECONDS = 300.0
_EXPIRING_THRESHOLD = 0.8  # notify "expiring" at 80% of TTL

# Canonical reason strings for approval timeout/expiry — used for structured
# detection in downstream layers (e.g. task error classification) so they are
# not coupled to English prose.
APPROVAL_EXPIRED_REASON = "approval expired"
APPROVAL_TIMED_OUT_REASON = "approval timed out"
APPROVAL_EXPIRY_REASONS = frozenset({APPROVAL_EXPIRED_REASON, APPROVAL_TIMED_OUT_REASON})
_PATH_ARG_KEYS = ("path", "cwd", "source_path", "dest_path", "target_path")
_PATH_LIST_KEYS = ("paths",)

_DESKTOP_FAMILY_PREFIXES = {
    "desktop_ax_": "desktop_ax_*",
    "desktop_view_": "desktop_view_*",
    "desktop_wait_": "desktop_wait_*",
    "desktop_control_": "desktop_control_*",
}
_DESKTOP_FAMILY_LABELS = {
    "desktop_ax_*": "Desktop AX Family",
    "desktop_view_*": "Desktop View Family",
    "desktop_wait_*": "Desktop Wait Family",
    "desktop_control_*": "Desktop Control Family",
}


def _desktop_family_pattern(tool_name: str) -> str:
    for prefix, pattern in _DESKTOP_FAMILY_PREFIXES.items():
        if tool_name.startswith(prefix):
            return pattern
    return tool_name


def _desktop_family_label(pattern: str) -> str:
    return _DESKTOP_FAMILY_LABELS.get(pattern, pattern)


@dataclass
class ToolRule:
    """A single tool policy rule.

    tool_pattern: glob pattern matching tool names (e.g. "shell.*", "browser_*", "*")
    action: what to do when matched
    reason: human-readable explanation
    """

    tool_pattern: str
    action: Action
    reason: str = ""

    def matches(self, tool_name: str) -> bool:
        return fnmatch.fnmatch(tool_name, self.tool_pattern)


@dataclass
class AgentPolicy:
    """Policy for a specific agent."""

    agent_name: str
    rules: List[ToolRule] = field(default_factory=list)
    fallback: Action = "deny"

    def evaluate(self, tool_name: str) -> Tuple[Action, str]:
        for rule in self.rules:
            if rule.matches(tool_name):
                return rule.action, rule.reason or f"matched rule: {rule.tool_pattern}"
        return self.fallback, f"fallback policy for agent '{self.agent_name}'"


@dataclass
class ApprovalRecord:
    """First-class tool approval with lifecycle and propagation metadata."""

    request_id: str
    tool_name: str
    agent_name: str
    args: Dict[str, Any]
    session_id: str
    reason: str
    created_at: float
    state: ApprovalState = "pending"
    scope: ApprovalScope = "single"
    tool_pattern: str = "*"
    path_scope: Optional[str] = None
    expires_at: Optional[float] = None
    propagate_to_subagents: bool = False
    approved: bool = False
    resolve_reason: str = ""
    resolved_at: Optional[float] = None
    source_request_id: Optional[str] = None
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.state not in {"pending", "expiring"}

    def is_expiring(self, now: Optional[float] = None) -> bool:
        if self.expires_at is None or self.created_at is None:
            return False
        t = now or time.time()
        if t >= self.expires_at:
            return False  # already expired
        ttl = self.expires_at - self.created_at
        if ttl <= 0:
            return False
        elapsed_ratio = (t - self.created_at) / ttl
        return elapsed_ratio >= _EXPIRING_THRESHOLD

    def is_expired(self, now: Optional[float] = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or time.time()) >= self.expires_at

    def matches_tool(self, tool_name: str) -> bool:
        return fnmatch.fnmatch(tool_name, self.tool_pattern)

    def matches_args(self, args: Mapping[str, Any]) -> bool:
        if not self.path_scope:
            return True
        requested_paths = _extract_path_candidates(args)
        if not requested_paths:
            return False
        for path in requested_paths:
            if _path_within_scope(path, self.path_scope):
                return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "tool_name": self.tool_name,
            "agent_name": self.agent_name,
            "args": self.args,
            "session_id": self.session_id,
            "reason": self.reason,
            "created_at": self.created_at,
            "state": self.state,
            "scope": self.scope,
            "tool_pattern": self.tool_pattern,
            "path_scope": self.path_scope,
            "expires_at": self.expires_at,
            "propagate_to_subagents": self.propagate_to_subagents,
            "resolved": self.resolved,
            "approved": self.approved,
            "resolve_reason": self.resolve_reason,
            "resolved_at": self.resolved_at,
            "source_request_id": self.source_request_id,
            "history": list(self.history),
        }

    def add_history(
        self,
        state: ApprovalState,
        *,
        reason: str = "",
        metadata: Optional[dict[str, Any]] = None,
        ts: Optional[float] = None,
    ) -> None:
        self.history.append(
            {
                "state": state,
                "reason": reason,
                "ts": ts if ts is not None else time.time(),
                "metadata": metadata or {},
            }
        )


# Default rules: broad allow for safe tools, approve for dangerous ones
_DEFAULT_RULES: List[ToolRule] = [
    ToolRule("memory_*", "allow", "memory operations are safe"),
    ToolRule("web_search", "allow", "web search is safe"),
    ToolRule("skill_list", "allow", "listing skills is safe"),
    ToolRule("skill_execute", "approve", "skill execution needs approval"),
    ToolRule("run_shell", "approve", "shell commands need approval"),
    ToolRule("shell_*", "approve", "shell commands need approval"),
    ToolRule("write_file", "approve", "file writes need approval"),
    ToolRule("read_file", "allow", "file reads are safe"),
    ToolRule("browser_*", "approve", "browser automation needs approval"),
    ToolRule("current_tab_info", "allow", "current tab metadata is low risk"),
    ToolRule("current_tab_*", "approve", "current tab automation needs approval"),
    ToolRule("control_ui_chat_*", "approve", "control UI browser automation needs approval"),
    ToolRule("desktop_view_windows", "allow", "desktop window listing is low risk"),
    ToolRule("desktop_view_frontmost_app", "allow", "frontmost app query is low risk"),
    ToolRule("desktop_wait_window", "allow", "waiting for a window is low risk"),
    ToolRule("desktop_ax_find", "allow", "selector-based accessibility lookup is low risk"),
    ToolRule("desktop_wait_element", "allow", "waiting for a selector is low risk"),
    ToolRule("desktop_runtime_status", "allow", "desktop runtime status is safe"),
    ToolRule("desktop_runtime_stop", "allow", "desktop emergency stop should stay available"),
    ToolRule("desktop_runtime_clear_stop", "approve", "clearing desktop emergency stop needs approval"),
    ToolRule("desktop_view_screenshot", "approve", "desktop screenshots need approval"),
    ToolRule("desktop_ax_snapshot", "approve", "accessibility snapshots need approval"),
    ToolRule("desktop_control_*", "approve", "desktop control needs approval"),
    ToolRule("desktop_control_launch_app", "approve", "desktop app launch needs approval"),
    ToolRule("desktop_control_focus_window", "approve", "desktop focus changes need approval"),
    ToolRule("desktop_control_scroll", "approve", "desktop scrolling needs approval"),
    ToolRule("stock_price", "allow", "stock price lookup is safe"),
    ToolRule("subagents_*", "approve", "subagent operations need approval"),
    ToolRule("sessions_*", "approve", "session operations need approval"),
]


def _normalize_scope(scope: Optional[str], default: ApprovalScope = "single") -> ApprovalScope:
    normalized = (scope or default).strip().lower()
    if normalized not in {"single", "session"}:
        return default
    return normalized  # type: ignore[return-value]


def _normalize_path(value: Any, *, cwd: Optional[str] = None) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute() and cwd:
        path = Path(cwd).expanduser() / path
    try:
        return str(path.resolve(strict=False))
    except Exception:
        return str(path)


def _extract_path_candidates(args: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(args, Mapping):
        return []

    cwd = _normalize_path(args.get("cwd")) if isinstance(args.get("cwd"), str) else None
    seen: set[str] = set()
    candidates: list[str] = []

    for key in _PATH_ARG_KEYS:
        candidate = _normalize_path(args.get(key), cwd=cwd)
        if candidate and candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)

    for key in _PATH_LIST_KEYS:
        values = args.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            candidate = _normalize_path(item, cwd=cwd)
            if candidate and candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)

    return candidates


def _derive_default_path_scope(args: Mapping[str, Any] | None) -> Optional[str]:
    candidates = _extract_path_candidates(args)
    return candidates[0] if candidates else None


def _path_within_scope(path: str, scope: str) -> bool:
    try:
        path_obj = Path(path).expanduser().resolve(strict=False)
        scope_obj = Path(scope).expanduser().resolve(strict=False)
        return path_obj == scope_obj or scope_obj in path_obj.parents
    except Exception:
        return path == scope or path.startswith(scope.rstrip("/") + "/")


class ToolPolicyEngine:
    """Evaluate tool execution permissions per agent."""

    def __init__(self) -> None:
        self._default_policy = AgentPolicy(
            agent_name="__default__",
            rules=list(_DEFAULT_RULES),
            fallback="deny",
        )
        self._agent_policies: Dict[str, AgentPolicy] = {}
        self._approvals: Dict[str, ApprovalRecord] = {}
        self._approval_waiters: Dict[str, asyncio.Future[tuple[bool, str]]] = {}
        self._notifier: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None

    @property
    def default_policy(self) -> AgentPolicy:
        return self._default_policy

    def register_agent_policy(self, policy: AgentPolicy) -> None:
        self._agent_policies[policy.agent_name] = policy

    def remove_agent_policy(self, agent_name: str) -> bool:
        return self._agent_policies.pop(agent_name, None) is not None

    def get_agent_policy(self, agent_name: str) -> Optional[AgentPolicy]:
        return self._agent_policies.get(agent_name)

    def set_notifier(
        self,
        notifier: Optional[Callable[[Dict[str, Any]], Awaitable[None]]],
    ) -> None:
        self._notifier = notifier

    @staticmethod
    def _notification_payload(
        approval: ApprovalRecord,
        *,
        event_type: str,
    ) -> Dict[str, Any]:
        payload = approval.to_dict()
        payload["event_type"] = event_type
        return payload

    async def _emit_notification(
        self,
        approval: ApprovalRecord,
        *,
        event_type: str,
    ) -> None:
        if self._notifier is None:
            return
        await self._notifier(self._notification_payload(approval, event_type=event_type))

    def _notify(
        self,
        approval: ApprovalRecord,
        *,
        event_type: str,
    ) -> None:
        if self._notifier is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._emit_notification(approval, event_type=event_type))

    def _notify_with_extra(
        self,
        approval: ApprovalRecord,
        *,
        event_type: str,
        extra: Dict[str, Any],
    ) -> None:
        if self._notifier is None:
            return
        payload = self._notification_payload(approval, event_type=event_type)
        payload.update(extra)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._notifier(payload))

    @staticmethod
    def _escalation_suggestions(approval: ApprovalRecord) -> List[Dict[str, str]]:
        """Build scope-upgrade suggestions for an expiring approval."""
        suggestions: List[Dict[str, str]] = []
        tool_name = approval.tool_name or ""
        if approval.scope == "single":
            suggestions.append({
                "strategy": "session_exact",
                "label": "Upgrade to Session Scope",
                "description": (
                    f"Re-approve '{tool_name}' with session scope so future "
                    "requests in this session are auto-approved."
                ),
                "tool_pattern": tool_name,
                "scope": "session",
            })
        if tool_name.startswith("desktop_"):
            family = _desktop_family_pattern(tool_name)
            if family and family != tool_name:
                suggestions.append({
                    "strategy": "family_session",
                    "label": f"Upgrade to {_desktop_family_label(family)}",
                    "description": (
                        f"Re-approve with pattern '{family}' at session scope."
                    ),
                    "tool_pattern": family,
                    "scope": "session",
                })
        return suggestions

    def list_policies(self) -> Dict[str, Any]:
        return {
            "default": {
                "rules": [
                    {"pattern": r.tool_pattern, "action": r.action, "reason": r.reason}
                    for r in self._default_policy.rules
                ],
                "fallback": self._default_policy.fallback,
            },
            "agents": {
                name: {
                    "rules": [
                        {"pattern": r.tool_pattern, "action": r.action, "reason": r.reason}
                        for r in p.rules
                    ],
                    "fallback": p.fallback,
                }
                for name, p in self._agent_policies.items()
            },
        }

    def evaluate(self, agent_name: str, tool_name: str) -> Tuple[Action, str]:
        """Evaluate whether a tool call is allowed."""
        agent_policy = self._agent_policies.get(agent_name)
        if agent_policy:
            action, reason = agent_policy.evaluate(tool_name)
            if action != "deny" or agent_policy.fallback != "deny":
                return action, reason
            for rule in agent_policy.rules:
                if rule.matches(tool_name):
                    return action, reason

        return self._default_policy.evaluate(tool_name)

    # ------------------------------------------------------------------
    # Approval state machine
    # ------------------------------------------------------------------

    def create_approval_request(
        self,
        request_id: str,
        tool_name: str,
        agent_name: str,
        args: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        reason: str = "",
        *,
        scope: ApprovalScope = "single",
        tool_pattern: Optional[str] = None,
        path_scope: Optional[str] = None,
        expires_at: Optional[float] = None,
        propagate_to_subagents: bool = False,
        state: ApprovalState = "pending",
        source_request_id: Optional[str] = None,
    ) -> ApprovalRecord:
        created_at = time.time()
        approval = ApprovalRecord(
            request_id=request_id,
            tool_name=tool_name,
            agent_name=agent_name,
            args=args or {},
            session_id=session_id or "",
            reason=reason,
            created_at=created_at,
            state=state,
            scope=scope,
            tool_pattern=tool_pattern or tool_name,
            path_scope=_normalize_path(path_scope) if path_scope else _derive_default_path_scope(args or {}),
            expires_at=expires_at if expires_at is not None else created_at + _DEFAULT_APPROVAL_TTL_SECONDS,
            propagate_to_subagents=propagate_to_subagents,
            source_request_id=source_request_id,
        )
        if approval.state in {"approved", "propagated"}:
            approval.approved = True
            approval.resolved_at = created_at
        elif approval.state == "denied":
            approval.approved = False
            approval.resolved_at = created_at
        approval.add_history(
            approval.state,
            reason=reason,
            metadata={
                "scope": approval.scope,
                "tool_pattern": approval.tool_pattern,
                "path_scope": approval.path_scope,
                "propagate_to_subagents": approval.propagate_to_subagents,
                "source_request_id": approval.source_request_id,
            },
            ts=created_at,
        )
        self._approvals[request_id] = approval
        return approval

    def resolve_approval(
        self,
        request_id: str,
        approved: bool,
        reason: str = "",
        *,
        scope: Optional[str] = None,
        tool_pattern: Optional[str] = None,
        path_scope: Optional[str] = None,
        expires_at: Optional[float] = None,
        propagate_to_subagents: Optional[bool] = None,
        history_metadata: Optional[dict[str, Any]] = None,
    ) -> Optional[ApprovalRecord]:
        approval = self._approvals.get(request_id)
        if approval is None or approval.state not in {"pending", "expiring"}:
            return None

        approval.scope = _normalize_scope(scope, approval.scope)
        if tool_pattern:
            approval.tool_pattern = tool_pattern
        if path_scope is not None:
            approval.path_scope = _normalize_path(path_scope)
        if expires_at is not None:
            approval.expires_at = expires_at
        if propagate_to_subagents is not None:
            approval.propagate_to_subagents = propagate_to_subagents

        approval.approved = approved
        approval.resolve_reason = reason
        approval.resolved_at = time.time()
        approval.state = "approved" if approved else "denied"
        approval.add_history(
            approval.state,
            reason=reason,
            metadata={
                "scope": approval.scope,
                "tool_pattern": approval.tool_pattern,
                "path_scope": approval.path_scope,
                "propagate_to_subagents": approval.propagate_to_subagents,
                **(history_metadata or {}),
            },
            ts=approval.resolved_at,
        )

        waiter = self._approval_waiters.pop(request_id, None)
        if waiter and not waiter.done():
            waiter.set_result((approved, reason))
        self._notify(approval, event_type="resolved")
        return approval

    def get_pending_approval(self, request_id: str) -> Optional[ApprovalRecord]:
        approval = self._approvals.get(request_id)
        if approval and approval.state == "pending":
            return approval
        return None

    def list_pending_approvals(
        self,
        session_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self.list_approvals(session_id=session_id, state="pending")

    def get_approval(
        self,
        request_id: str,
        *,
        include_expired: bool = True,
    ) -> Optional[Dict[str, Any]]:
        self.cleanup_expired()
        approval = self._approvals.get(request_id)
        if approval is None:
            return None
        if not include_expired and approval.state == "expired":
            return None
        return approval.to_dict()

    def list_approvals(
        self,
        *,
        session_id: Optional[str] = None,
        state: Optional[str] = None,
        include_expired: bool = False,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        self.cleanup_expired()
        page_size = limit
        if page_size is None:
            approvals = list(self._approvals.values())
            if session_id:
                approvals = [item for item in approvals if item.session_id == session_id]
            if state and state.lower() != "all":
                approvals = [item for item in approvals if item.state == state]
            if not include_expired:
                approvals = [item for item in approvals if item.state != "expired"]
            page_size = max(1, len(approvals) or 1)
        result = self.query_approvals(
            session_id=session_id,
            state=state,
            include_expired=include_expired,
            page=1,
            page_size=page_size,
        )
        return result["approvals"]

    @staticmethod
    def _approval_search_text(approval: ApprovalRecord) -> str:
        payload = approval.to_dict()
        parts = [
            payload.get("request_id"),
            payload.get("source_request_id"),
            payload.get("tool_name"),
            payload.get("tool_pattern"),
            payload.get("agent_name"),
            payload.get("session_id"),
            payload.get("reason"),
            payload.get("resolve_reason"),
            payload.get("path_scope"),
            payload.get("scope"),
            payload.get("state"),
        ]
        if payload.get("args"):
            parts.append(json.dumps(payload["args"], ensure_ascii=False, sort_keys=True))
        if payload.get("history"):
            parts.append(json.dumps(payload["history"], ensure_ascii=False, sort_keys=True))
        return " ".join(str(part).strip() for part in parts if part).lower()

    def query_approvals(
        self,
        *,
        session_id: Optional[str] = None,
        state: Optional[str] = None,
        include_expired: bool = False,
        q: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        self.cleanup_expired()
        approvals = list(self._approvals.values())
        if session_id:
            approvals = [item for item in approvals if item.session_id == session_id]
        if state and state.lower() != "all":
            if state == "pending":
                approvals = [item for item in approvals if item.state in {"pending", "expiring"}]
            else:
                approvals = [item for item in approvals if item.state == state]
        if not include_expired:
            approvals = [item for item in approvals if item.state != "expired"]
        query_text = (q or "").strip().lower()
        if query_text:
            approvals = [
                item
                for item in approvals
                if query_text in self._approval_search_text(item)
            ]
        approvals.sort(key=lambda item: item.created_at, reverse=True)
        resolved_page = max(1, int(page or 1))
        resolved_page_size = max(1, min(int(page_size or 20), 100))
        start = (resolved_page - 1) * resolved_page_size
        page_items = approvals[start:start + resolved_page_size]
        return {
            "approvals": [item.to_dict() for item in page_items],
            "pagination": {
                "page": resolved_page,
                "page_size": resolved_page_size,
                "total": len(approvals),
                "has_more": start + len(page_items) < len(approvals),
            },
            "filters": {
                "session_id": session_id,
                "state": state,
                "include_expired": include_expired,
                "q": query_text,
            },
        }

    def cleanup_expired(self, max_age: float = _DEFAULT_APPROVAL_TTL_SECONDS) -> int:
        """Expire old approval requests and grants without deleting history.

        Also transitions pending approvals to "expiring" at the 80% TTL mark
        and attaches escalation suggestions so the UI can offer scope upgrades.
        """
        now = time.time()
        expired_count = 0
        for approval in self._approvals.values():
            # --- expiring transition (pending → expiring) ---
            if approval.state == "pending" and approval.is_expiring(now):
                approval.state = "expiring"
                suggestions = self._escalation_suggestions(approval)
                approval.add_history(
                    "expiring",
                    reason="approaching TTL limit",
                    metadata={"escalation_suggestions": suggestions},
                    ts=now,
                )
                self._notify_with_extra(
                    approval,
                    event_type="expiring",
                    extra={"escalation_suggestions": suggestions},
                )
                continue

            # --- expired transition ---
            expired = False
            if approval.state in {"pending", "expiring"} and (
                now - approval.created_at > max_age or approval.is_expired(now)
            ):
                expired = True
            elif approval.state in {"approved", "propagated"} and approval.is_expired(now):
                expired = True
            if not expired or approval.state == "expired":
                continue

            approval.state = "expired"
            approval.approved = False
            approval.resolve_reason = approval.resolve_reason or APPROVAL_EXPIRED_REASON
            approval.resolved_at = approval.resolved_at or now
            approval.add_history(
                "expired",
                reason=approval.resolve_reason,
                metadata={
                    "scope": approval.scope,
                    "tool_pattern": approval.tool_pattern,
                    "path_scope": approval.path_scope,
                    "propagate_to_subagents": approval.propagate_to_subagents,
                },
                ts=approval.resolved_at,
            )
            expired_count += 1

            waiter = self._approval_waiters.pop(approval.request_id, None)
            if waiter and not waiter.done():
                waiter.set_result((False, APPROVAL_EXPIRED_REASON))
            self._notify(approval, event_type="expired")
        return expired_count

    def propagate_approvals_to_session(
        self,
        *,
        source_session_id: str,
        target_session_id: str,
        agent_name: str = "",
    ) -> List[Dict[str, Any]]:
        self.cleanup_expired()
        if not source_session_id or not target_session_id or source_session_id == target_session_id:
            return []

        propagated: list[ApprovalRecord] = []
        for approval in sorted(self._approvals.values(), key=lambda item: item.created_at):
            if approval.session_id != source_session_id:
                continue
            if approval.state not in {"approved", "propagated"}:
                continue
            if approval.scope != "session" or not approval.propagate_to_subagents:
                continue
            if approval.is_expired():
                continue
            if self._has_existing_propagation(
                source_request_id=approval.request_id,
                target_session_id=target_session_id,
            ):
                continue
            created = self.create_approval_request(
                    request_id=f"apg_{uuid.uuid4().hex[:12]}",
                    tool_name=approval.tool_name,
                    agent_name=agent_name or approval.agent_name,
                    args=dict(approval.args),
                    session_id=target_session_id,
                    reason=approval.reason,
                    scope=approval.scope,
                    tool_pattern=approval.tool_pattern,
                    path_scope=approval.path_scope,
                    expires_at=approval.expires_at,
                    propagate_to_subagents=approval.propagate_to_subagents,
                    state="propagated",
                    source_request_id=approval.request_id,
                )
            propagated.append(created)
            self._notify(created, event_type="propagated")
        return [item.to_dict() for item in propagated]

    def _has_existing_propagation(
        self,
        *,
        source_request_id: str,
        target_session_id: str,
    ) -> bool:
        for approval in self._approvals.values():
            if approval.session_id != target_session_id:
                continue
            if approval.state not in {"approved", "propagated"}:
                continue
            if approval.source_request_id == source_request_id:
                return True
        return False

    def _find_matching_approval(
        self,
        *,
        tool_name: str,
        session_id: str,
        args: Optional[Mapping[str, Any]] = None,
    ) -> Optional[ApprovalRecord]:
        self.cleanup_expired()
        candidates = [
            approval
            for approval in self._approvals.values()
            if approval.session_id == session_id
            and approval.state in {"approved", "propagated"}
            and approval.scope == "session"
            and approval.matches_tool(tool_name)
            and approval.matches_args(args or {})
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (
                item.state != "propagated",
                item.resolved_at or item.created_at,
            ),
            reverse=True,
        )
        return candidates[0]

    async def request_approval(
        self,
        *,
        tool_name: str,
        agent_name: str,
        args: Optional[Dict[str, Any]] = None,
        session_id: str,
        reason: str = "",
        timeout: float = _DEFAULT_APPROVAL_TTL_SECONDS,
    ) -> Tuple[bool, str]:
        approved, resolve_reason, _request_id = await self.request_approval_with_id(
            tool_name=tool_name,
            agent_name=agent_name,
            args=args,
            session_id=session_id,
            reason=reason,
            timeout=timeout,
        )
        return approved, resolve_reason

    async def request_approval_with_id(
        self,
        *,
        tool_name: str,
        agent_name: str,
        args: Optional[Dict[str, Any]] = None,
        session_id: str,
        reason: str = "",
        timeout: float = _DEFAULT_APPROVAL_TTL_SECONDS,
    ) -> Tuple[bool, str, str]:
        """Request approval and wait for a user response."""
        if not session_id:
            return False, "approval requires a valid session_id", ""

        reusable = self._find_matching_approval(
            tool_name=tool_name,
            session_id=session_id,
            args=args or {},
        )
        if reusable is not None:
            return True, f"reused {reusable.state} approval", reusable.request_id

        if self._notifier is None:
            return False, "approval channel is unavailable", ""

        request_id = uuid.uuid4().hex[:12]
        approval = self.create_approval_request(
            request_id=request_id,
            tool_name=tool_name,
            agent_name=agent_name,
            args=args,
            session_id=session_id,
            reason=reason,
            scope="single",
            tool_pattern=tool_name,
            path_scope=_derive_default_path_scope(args or {}),
            expires_at=time.time() + max(timeout, 1.0),
            propagate_to_subagents=False,
        )

        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[tuple[bool, str]] = loop.create_future()
        self._approval_waiters[request_id] = waiter

        try:
            await self._emit_notification(approval, event_type="created")
        except Exception as exc:
            self._approvals.pop(request_id, None)
            self._approval_waiters.pop(request_id, None)
            return False, f"failed to deliver approval request: {exc}", request_id

        try:
            approved, resolve_reason = await asyncio.wait_for(waiter, timeout=timeout)
        except asyncio.TimeoutError:
            self.cleanup_expired(max_age=timeout)
            return False, APPROVAL_TIMED_OUT_REASON, request_id
        except asyncio.CancelledError:
            self._approval_waiters.pop(request_id, None)
            raise

        return approved, resolve_reason, request_id


# Global singleton
_engine: Optional[ToolPolicyEngine] = None


def get_tool_policy_engine() -> ToolPolicyEngine:
    global _engine
    if _engine is None:
        _engine = ToolPolicyEngine()
    return _engine
