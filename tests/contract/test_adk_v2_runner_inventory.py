from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

# A Runner is the ADK application/session entrypoint. Multi-stage orchestration
# must give it a Workflow root. A genuinely standalone Agent is itself a valid
# ADK v2 root and must not be wrapped in a one-node placeholder Workflow. Each
# value records (classification, Runner agent expression).
EXPECTED_RUNNER_ROOTS = {
    ("src/control_loop/root_workflow.py", "ControlLoop.run"): (
        "workflow_root",
        "workflow",
    ),
    (
        "src/gateway/server.py",
        "GatewayServer._run_gateway_conversation_workflow",
    ): ("workflow_root", "workflow"),
    (
        "src/gateway/server.py",
        "GatewayServer._select_route_for_message",
    ): ("workflow_root", "workflow"),
    (
        "src/intelligence/missionos_adk_v2_hitl.py",
        "start_missionos_canonical_approval_hitl",
    ): ("workflow_root", "workflow"),
    (
        "src/intelligence/missionos_adk_v2_hitl.py",
        "resume_missionos_canonical_approval_hitl",
    ): ("workflow_root", "workflow"),
    (
        "src/intelligence/missionos_adk_v2_shadow_graph.py",
        "_run_missionos_conversation_graph_async",
    ): ("workflow_root", "workflow"),
    ("src/main.py", "_run_cli"): ("single_agent_root", "root_agent"),
    ("src/main.py", "_run_channels"): ("single_agent_root", "root_agent"),
    (
        "src/intelligence/llm_dialogue_router.py",
        "_invoke_adk_gemini_async",
    ): ("single_agent_root", "agent"),
    (
        "src/intelligence/llm_repair_planner.py",
        "_invoke_adk_gemini_repair_text_async",
    ): ("single_agent_root", "agent"),
    (
        "src/intelligence/llm_response_planner.py",
        "_invoke_adk_gemini_response_text_async",
    ): ("single_agent_root", "agent"),
    (
        "src/intelligence/missionos_agent_runtime.py",
        "_invoke_adk_agent_text_async",
    ): ("single_agent_root", "agent"),
    (
        "src/intelligence/missionos_agent_runtime.py",
        "_invoke_runtime_recovery_agent_text_with_tools_async",
    ): ("single_agent_root", "agent"),
    (
        "src/intelligence/missionos_chief_planner_tools.py",
        "_invoke_chief_route_function_tool_async",
    ): ("single_agent_root", "agent"),
    (
        "src/intelligence/real_hardware_arm_disarm_planner.py",
        "_invoke_adk_gemini_response_text_async",
    ): ("single_agent_root", "agent"),
    (
        "src/intelligence/turtlebot3_perception_sidecar.py",
        "_invoke_adk_perception_response_async",
    ): ("single_agent_root", "agent"),
    (
        "src/intelligence/turtlebot3_recovery_planner.py",
        "_invoke_adk_response_text_async",
    ): ("single_agent_root", "agent"),
    ("src/tools/subagents.py", "SubagentManager._worker_loop"): (
        "single_agent_root",
        "resolved_agent",
    ),
}


class _RunnerCallVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.calls: list[tuple[str, str]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == "Runner":
            agent_value = next(
                (
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg == "agent"
                ),
                node.args[0] if node.args else None,
            )
            self.calls.append(
                (
                    ".".join(self.scope) or "<module>",
                    ast.unparse(agent_value) if agent_value is not None else "<missing>",
                )
            )
        self.generic_visit(node)


def _discover_runner_roots() -> dict[tuple[str, str], str]:
    discovered: dict[tuple[str, str], str] = {}
    for source_path in sorted((REPO_ROOT / "src").rglob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        visitor = _RunnerCallVisitor()
        visitor.visit(tree)
        relative_path = source_path.relative_to(REPO_ROOT).as_posix()
        for scope, agent_expression in visitor.calls:
            key = (relative_path, scope)
            assert key not in discovered, f"duplicate Runner scope: {key}"
            discovered[key] = agent_expression
    return discovered


def test_every_production_runner_has_an_explicit_v2_root_classification() -> None:
    discovered = _discover_runner_roots()

    assert set(discovered) == set(EXPECTED_RUNNER_ROOTS)
    assert discovered == {
        key: agent_expression
        for key, (_classification, agent_expression) in EXPECTED_RUNNER_ROOTS.items()
    }
    assert {
        classification for classification, _agent in EXPECTED_RUNNER_ROOTS.values()
    } == {"workflow_root", "single_agent_root"}
    assert sum(
        classification == "workflow_root"
        for classification, _agent in EXPECTED_RUNNER_ROOTS.values()
    ) == 6
    assert sum(
        classification == "single_agent_root"
        for classification, _agent in EXPECTED_RUNNER_ROOTS.values()
    ) == 12
