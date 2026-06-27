"""
Skills ツール
ロード済みスキルの一覧取得と実行
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from google.adk.agents.context import Context as ToolContext

from src.runtime.capability_registry import (
    invoke_runtime_capability,
    list_runtime_capabilities,
    list_runtime_resources,
    read_runtime_resource,
)
from src.skills.base import get_skill_registry
from src.skills.runtime import ensure_skills_loaded
from src.tools.subagents import sessions_spawn_dynamic


async def skill_list() -> Dict[str, Any]:
    """ロード済みスキル一覧を返す"""
    await ensure_skills_loaded()
    registry = get_skill_registry()
    items = []
    for meta in sorted(
        registry.list_skills(),
        key=lambda item: (0 if item.name.startswith("promoted/") else 1, item.name),
    ):
        items.append(
            {
                "name": meta.name,
                "description": meta.description,
                "version": meta.version,
                "author": meta.author,
                "tags": meta.tags,
            }
        )
    return {"count": len(items), "skills": items}


async def skill_execute(name: str, params_json: str = "{}") -> Dict[str, Any]:
    """
    スキルを実行する（内容確認・メタ情報取得用）

    Args:
        name: スキル名
        params_json: スキル引数(JSON文字列)
    """
    await ensure_skills_loaded()
    registry = get_skill_registry()
    skill = registry.get_skill(name)
    if not skill:
        return {"ok": False, "message": f"Skill not found: {name}"}

    try:
        params = json.loads(params_json) if params_json.strip() else {}
        if not isinstance(params, dict):
            return {"ok": False, "message": "params_json must decode to object"}
    except json.JSONDecodeError as exc:
        return {"ok": False, "message": f"Invalid params_json: {exc}"}

    is_valid, reason = await skill.validate_input(**params)
    if not is_valid:
        return {"ok": False, "message": reason or "Invalid input"}

    result = await skill.execute(**params)
    return {"ok": True, "skill": name, "result": result}


async def skill_spawn(
    name: str,
    task: str,
    mcp_servers: str = "[]",
    mode: str = "run",
    run_timeout_seconds: int = 0,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """
    スキルの内容を instruction とした動的 Agent を生成してタスクを実行する。
    複雑・長時間のタスクや、スキル特化の動作が必要な場合に使う。
    この関数は skill_execute() と違い、execute() の返り値 `content` を
    instruction として取り出して spawn に渡す。

    Args:
        name: スキル名
        task: スキル Agent に与えるタスク
        mcp_servers: 追加 MCP サーバー設定(JSON配列文字列)
        mode: "run"（1回実行）/ "session"（継続セッション）
        run_timeout_seconds: タイムアウト秒数（0=無制限）
    """
    await ensure_skills_loaded()
    registry = get_skill_registry()
    skill = registry.get_skill(name)
    if not skill:
        return {"ok": False, "message": f"Skill not found: {name}"}

    # スキルのコンテンツを取得して instruction として使用
    result = await skill.execute(task=task)
    instruction = result.get("content", "").strip()
    if not instruction:
        return {"ok": False, "message": f"Skill has no content to use as instruction: {name}"}

    spawn = await sessions_spawn_dynamic(
        task=task,
        instruction=instruction,
        mcp_servers=mcp_servers,
        mode=mode,
        run_timeout_seconds=run_timeout_seconds,
        tool_context=tool_context,
    )
    ok = spawn.get("status") != "error"
    return {
        "ok": ok,
        "skill": name,
        "spawn": spawn,
        **({"message": spawn.get("error")} if not ok else {}),
    }


async def resource_list() -> Dict[str, Any]:
    """Runtime substrate resources (skills / bridges) を列挙する。"""
    return await list_runtime_resources()


async def resource_read(resource_id: str, refresh: bool = False) -> Dict[str, Any]:
    """Runtime substrate resource を読む。"""
    return await read_runtime_resource(resource_id, refresh=refresh)


async def capability_list(refresh: bool = False) -> Dict[str, Any]:
    """Runtime substrate capability を列挙する。"""
    return await list_runtime_capabilities(refresh=refresh)


async def capability_invoke(
    name: str,
    params_json: str = "{}",
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Runtime substrate capability を呼び出す。"""
    try:
        params = json.loads(params_json) if params_json.strip() else {}
        if not isinstance(params, dict):
            return {
                "success": False,
                "capability": name,
                "error": "params_json must decode to object",
            }
    except json.JSONDecodeError as exc:
        return {
            "success": False,
            "capability": name,
            "error": f"Invalid params_json: {exc}",
        }

    return await invoke_runtime_capability(name, params=params, tool_context=tool_context)
