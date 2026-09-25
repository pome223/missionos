"""Read-only indoor Go2 task views, using the Gateway's task artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import json
import math
from typing import Any

KIND = "go2_delivery_execution"
TERMINAL = {
    "completed",
    "needs_attention",
    "canceled",
    "cancelled",
    "returned_undelivered",
    "rejected",
    "failed",
    "blocked",
}
# The fixed office geometry in simulators/go2_delivery/mujoco_backend.py.
OFFICE_OBSTACLES = (
    (-4.0, 0.0, 0.1, 3.0),
    (4.0, 0.0, 0.1, 3.0),
    (0.0, -3.0, 4.0, 0.1),
    (0.0, 3.0, 4.0, 0.1),
    (0.0, 0.0, 0.09, 1.0),
    (2.5, 1.9, 0.6, 0.35),
)


def is_go2_task(task: dict[str, Any]) -> bool:
    return task.get("kind") == KIND


def _xy(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if any(
        isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item)
        for item in value
    ):
        return None
    return float(value[0]), float(value[1])


def _sources(task: dict[str, Any]):
    artifacts = task.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    proposal = artifacts.get("go2_delivery_proposal")
    proposal = proposal if isinstance(proposal, dict) else {}
    result = artifacts.get("go2_delivery_result")
    result = result if isinstance(result, dict) else {}
    snapshot = artifacts.get("go2_delivery_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    avoidance = artifacts.get("go2_local_avoidance")
    avoidance = avoidance if isinstance(avoidance, dict) else {}
    return artifacts, proposal, result, snapshot, avoidance


def _receipt_observed(result: dict[str, Any]) -> bool:
    receipt = result.get("receipt")
    return (
        isinstance(receipt, dict)
        and receipt.get("source") == "simulation_recipient"
        and receipt.get("received") is True
    )


def _delivery_and_return_verified(task: dict[str, Any], result: dict[str, Any]) -> bool:
    events = result.get("events")
    return (
        task.get("status") == "completed"
        and result.get("completion_claimed") is True
        and _receipt_observed(result)
        and isinstance(events, list)
        and any(
            isinstance(event, dict) and event.get("event") == "terminal_hold_verified"
            for event in events
        )
    )


def supervision_summary(task: dict[str, Any]) -> dict[str, Any]:
    artifacts, proposal, result, _, _ = _sources(task)
    entries = artifacts.get("go2_supervision_decisions") or result.get("supervision_decisions", [])
    decisions = []
    for entry in entries:
        invocation = entry.get("invocation", {})
        action = entry.get("proposal", {})
        observed = invocation.get("standalone_runner_invoked") is True and invocation.get(
            "response_sha256"
        ) not in (
            None,
            "",
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        decisions.append(
            dict(
                observation_id=entry.get("observation", {}).get("observation_id"),
                action=action.get("action", "unknown"),
                rationale=action.get("rationale", ""),
                model=invocation.get("model_id"),
                response_observed=observed,
                rules=(
                    "allowed"
                    if entry.get("judge_status") == "valid"
                    and entry.get("rule_blocking_reasons") == []
                    else "blocked_or_unverified"
                ),
            )
        )
    return dict(
        mode=proposal.get("plan", {}).get("supervision_mode", "rules"),
        configured_model=(proposal.get("supervisor") or {}).get("model_id"),
        observed_responses=sum(d["response_observed"] for d in decisions),
        decisions=decisions,
    )


def supervision_lines(task: dict[str, Any]) -> list[str]:
    summary = supervision_summary(task)
    lines = [
        f"Supervision: {summary['mode']}; configured_model={summary['configured_model'] or '-'}; "
        f"observed_responses={summary['observed_responses']}"
    ]
    for decision in summary["decisions"]:
        lines.append(
            f"Agent {decision['observation_id']}: {decision['action']}; Rules={decision['rules']}"
            f" — {decision['rationale']}"
        )
    return lines


def summary_lines(task: dict[str, Any]) -> list[str]:
    artifacts, proposal, result, snapshot, avoidance = _sources(task)
    task_id = str(task.get("task_id") or "-")
    status = str(task.get("status") or "-")
    position = _xy(snapshot.get("xy"))
    approved = isinstance(artifacts.get("go2_delivery_approval"), dict)
    completion = _delivery_and_return_verified(task, result)
    receipt = _receipt_observed(result)
    dynamic = result.get("dynamic_avoidance")
    dynamic = dynamic if isinstance(dynamic, dict) else {}
    lines = [
        f"Task: {task_id}",
        f"Status: {status}; phase={snapshot.get('phase') or artifacts.get('go2_delivery_phase') or '-'}",
        f"Scenario: {proposal.get('scenario') or '-'}; operator_approved={approved}",
        "Position: " + (f"({position[0]:.2f}, {position[1]:.2f}) m" if position else "unobserved"),
        f"Simulator time: {snapshot.get('sim_time_s') if snapshot.get('sim_time_s') is not None else '-'} s",
        f"Local avoidance: {avoidance.get('event') or '-'}; yields={dynamic.get('yield_count', '-')}",
        f"Receipt observed: {receipt}; simulated delivery and return verified: {completion}",
        f"Moving-obstacle contacts: {dynamic.get('contact_physics_steps', '-')}",
        "Boundary: simulated Go2 only; physical_execution=False; view is read-only",
    ]
    lines.extend(supervision_lines(task))
    if task.get("error"):
        lines.append(f"Error: {task['error']}")
    return lines


def local_map_model(task: dict[str, Any]) -> dict[str, Any]:
    artifacts, proposal, result, snapshot, _ = _sources(task)
    trail = artifacts.get("go2_trajectory")
    trail = [_xy(point) for point in trail] if isinstance(trail, list) else []
    trail = [point for point in trail if point is not None]
    current = _xy(snapshot.get("xy"))
    if current is not None and (not trail or trail[-1] != current):
        trail.append(current)
    moving = snapshot.get("moving_obstacle")
    obstacle = _xy(moving.get("xy")) if isinstance(moving, dict) else None
    plan = proposal.get("plan")
    plan = plan if isinstance(plan, dict) else {}
    home = _xy(plan.get("home_xy")) or (-2.5, 0.0)
    destination = _xy(plan.get("destination_xy")) or (2.5, 0.0)
    return dict(
        schema_version="missionos_go2_indoor_map.v1",
        task_id=str(task.get("task_id") or ""),
        status=str(task.get("status") or ""),
        generated_at=datetime.now(timezone.utc).isoformat(),
        home=home,
        destination=destination,
        trail=trail,
        current=current,
        moving_obstacle=obstacle,
        static_obstacles=OFFICE_OBSTACLES,
        receipt_observed=_receipt_observed(result),
        delivery_and_return_verified=_delivery_and_return_verified(task, result),
        supervision=supervision_summary(task),
        supervision_lines=supervision_lines(task),
        source="Gateway task artifacts and simulator known map",
        live=False,
    )


def terminal_map(model: dict[str, Any], width: int = 49, height: int = 21) -> str:
    cells = [[" " for _ in range(width)] for _ in range(height)]

    def pixel(xy):
        x, y = xy
        return round((x + 4) / 8 * (width - 1)), round((3 - y) / 6 * (height - 1))

    for oy, row in enumerate(cells):
        y = 3 - oy * 6 / (height - 1)
        for ox in range(width):
            x = -4 + ox * 8 / (width - 1)
            if any(abs(x - bx) <= sx and abs(y - by) <= sy for bx, by, sx, sy in OFFICE_OBSTACLES):
                row[ox] = "#"
    for point in model["trail"]:
        x, y = pixel(point)
        if 0 <= x < width and 0 <= y < height:
            cells[y][x] = "."
    for point, marker in (
        (model["home"], "H"),
        (model["destination"], "D"),
        (model["moving_obstacle"], "O"),
        (model["current"], "R"),
    ):
        if point is not None:
            x, y = pixel(point)
            if 0 <= x < width and 0 <= y < height:
                cells[y][x] = marker
    return "\n".join("".join(row) for row in cells)


def html_map(model: dict[str, Any]) -> str:
    width, height = 800, 580

    def px(point):
        x, y = point
        return 70 + (x + 4) / 8 * (width - 140), 60 + (3 - y) / 6 * (height - 120)

    shapes = []
    for x, y, sx, sy in model["static_obstacles"]:
        left, top = px((x - sx, y + sy))
        right, bottom = px((x + sx, y - sy))
        shapes.append(
            f'<rect x="{left:.1f}" y="{top:.1f}" width="{right - left:.1f}" height="{bottom - top:.1f}" class="wall"/>'
        )
    trail = model["trail"]
    if len(trail) >= 2:
        points = " ".join(f"{px(point)[0]:.1f},{px(point)[1]:.1f}" for point in trail)
        shapes.append(f'<polyline points="{points}" class="trail"/>')
    for key, label, klass in (
        ("home", "H", "home"),
        ("destination", "D", "destination"),
        ("moving_obstacle", "O", "obstacle"),
        ("current", "R", "robot"),
    ):
        point = model[key]
        if point is not None:
            x, y = px(point)
            shapes.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="11" class="{klass}"/><text x="{x:.1f}" y="{y + 4:.1f}" text-anchor="middle">{label}</text>'
            )
    supervision = "".join(f"<p>{escape(line)}</p>" for line in model.get("supervision_lines", []))
    title = escape(model["task_id"])
    status = escape(model["status"])
    data = json.dumps(model, ensure_ascii=False).replace("<", "\\u003c")
    return f"""<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MissionOS Go2 indoor map — {title}</title>
<style>body{{background:#101925;color:#e6f0fb;font:16px system-ui;margin:24px}}main{{max-width:920px;margin:auto}}svg{{width:100%;background:#17263a;border:1px solid #42607d;border-radius:12px}}.wall{{fill:#586a7a}}.trail{{fill:none;stroke:#5bd6b3;stroke-width:3}}.home{{fill:#3088c7}}.destination{{fill:#57b68d}}.obstacle{{fill:#e06a51}}.robot{{fill:#f4d36b}}text{{fill:#111;font:bold 13px system-ui}}.muted{{color:#a8bdd0}}</style>
<main><h1>Go2 屋内配送マップ</h1><p>配送ID: <code>{title}</code> · 状態: {status}</p>{supervision}<svg viewBox="0 0 {width} {height}" role="img" aria-label="Go2 indoor map">{"".join(shapes)}</svg>
<p>H 受付 · D 会議室A · R Go2現在位置 · O 動く障害物 · 緑線 観測された走行軌跡</p><p class="muted">この表示はシミュレータの既知の室内地図とGatewayに保存された観測値です。予測・承認・実行・配送完了の証明ではありません。</p>
<script id="go2-map-data" type="application/json">{data}</script></main></html>"""
