"""Goal-level Go2 delivery through the normal MissionOS conversation route.

The catalog parser makes a proposal only. Server-owned context and task records
bind explicit operator approval to the exact plan and simulator inputs. Workers
run the opt-in simulator in its own Python environment; no hardware SDK is used.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Callable
import uuid

from src.runtime.go2_delivery_mission import Go2DeliveryPlan
from src.runtime.go2_supervision import MAX_DECISIONS, digest, supervision_envelope
from src.runtime.task_store import TaskStore, get_task_store


KIND = "go2_delivery_execution"
TARGET = "go2_mujoco_delivery"
ACTIVE = {"starting", "running", "cancel_requested"}
OTHER_ROBOTS = r"(turtlebot|タートルボット|nova[ -]?carter|px4|drone|ドローン|gr00t)"
COMMANDS = {
    "Approve the current MissionOS plan.": "approve",
    "Reject the current MissionOS plan.": "reject",
    "Run the current bounded action through the MissionOS execution gate.": "execute",
    "承認して": "approve",
    "拒否して": "reject",
    "実行して": "execute",
    "/approve": "approve",
    "承認": "approve",
    "承認する": "approve",
    "承認して開始": "approve_and_run",
    "/run": "execute",
    "開始": "execute",
    "/status": "status",
    "状況": "status",
    "今どうなってる？": "status",
    "/reject": "reject",
    "却下": "reject",
    "/cancel": "cancel",
    "中止": "cancel",
}
PHASES = {
    "approved": "配送承認済み",
    "outbound": "会議室Aへ移動中",
    "waiting_to_retry": "通路を確認して再計画中",
    "awaiting_receipt": "受領待ち",
    "returning": "受付へ帰還中",
    "verifying_return": "帰還後の状態を確認中",
    "completed": "模擬受領と帰還を確認・完了",
    "needs_attention": "停止・対応が必要",
    "canceled": "配送を中止",
    "cancel_requested": "停止を確認中",
    "proposed": "配送計画の承認待ち",
    "starting": "シミュレータを準備中",
    "running": "配送を実行中",
    "rejected": "配送計画を却下",
    "Preparing": "シミュレータを準備中",
    "Outbound": "会議室Aへ移動中",
    "Returning": "受付へ帰還中",
    "Awaiting receipt": "受領待ち",
    "Waiting: route blocked": "通路閉塞・停止して再計画中",
    "Verifying return": "帰還後の状態を確認中",
    "Supervisor judging": "Agentが状況を判断中",
    "Yielding to moving obstacle": "動く障害物に道を譲っています",
    "Waiting for passage": "通路が開くのを待機中",
    "returned_undelivered": "配送を見送り、受付への帰還を確認",
}


def requested(text: str) -> bool:
    return bool(
        not re.search(OTHER_ROBOTS, text, re.I)
        and re.search(r"(go2|unitree|犬|会議室|meeting\s*room)", text, re.I)
        and re.search(r"(配送|配達|届け|deliver)", text, re.I)
    )


def _digest(value: dict) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Go2ChatService:
    def __init__(self, store: TaskStore):
        self.store = store
        self.lock = threading.RLock()
        self.workers: dict[str, subprocess.Popen] = {}
        self.active: str | None = None
        self.root = Path(__file__).resolve().parents[2]
        self.outputs = Path(
            os.environ.get("MISSIONOS_GO2_OUTPUT_ROOT", "output/go2-chat")
        ).resolve()
        self.cache = Path(
            os.environ.get("GO2_DELIVERY_CACHE", str(Path.home() / ".cache/missionos-go2-delivery"))
        )
        for task in self.store.list(kind=KIND, limit=100):
            if task["status"] in ACTIVE:
                self.store.update(
                    task["task_id"],
                    status="needs_attention",
                    error="Gateway was restarted; the prior execution is not resumed automatically.",
                )

    def inputs(self):
        paths = {
            "python": self.cache / "venv/bin/python",
            "model": self.cache / "rl-sar-zoo/go2_description/mjcf/go2.xml",
            "policy": self.cache / "rl-sar/policy/go2/robot_lab/policy.pt",
        }
        if not all(p.is_file() for p in paths.values()):
            raise ValueError(
                "Go2の実行環境が未準備です。再実行手順でシミュレータ環境を準備してください。"
            )
        return paths

    def input_hashes(self):
        paths = self.inputs()
        return {name: sha256(paths[name].read_bytes()).hexdigest() for name in ("model", "policy")}

    def plan(self, text, session_id, scenario, supervision_mode=None):
        if not re.search(r"(会議室\s*[AＡ](?![A-Za-zＡ-Ｚ])|meeting\s*room\s*a\b)", text, re.I):
            raise ValueError(
                "この試作の配送先は会議室Aです。『会議室Aへ届けて』と指定してください。"
            )
        if re.search(
            r"(会議室\s*[B-ZＢ-Ｚ]|屋外|実機|階段|エレベータ|\d\s*階|door|elevator|outdoor)",
            text,
            re.I,
        ):
            raise ValueError("今回は同じ階の屋内シミュレーションだけに対応しています。")
        if scenario not in (
            "baseline",
            "blocked_passage",
            "all_blocked",
            "temporary_blockage",
            "moving_obstacle",
        ):
            raise ValueError("未対応のシナリオです。")
        mode = supervision_mode or os.getenv("MISSIONOS_GO2_SUPERVISION_MODE", "rules")
        if mode not in ("rules", "agent"):
            raise ValueError("未対応の管制方法です。")
        agent_config = None
        if mode == "agent":
            from src.intelligence.go2_supervisor import configuration

            agent_config = configuration()
        input_hashes = self.input_hashes()
        for prior in self.store.list(kind=KIND, owner_session_id=session_id, limit=100):
            if prior["status"] in ACTIVE:
                raise ValueError("この会話の配送を実行中です。状況確認または中止を選んでください。")
            if prior["status"] in ("proposed", "approved"):
                self.store.update(prior["task_id"], status="superseded")
        identity = f"go2_{uuid.uuid4().hex[:16]}"
        plan = Go2DeliveryPlan(mission_id=identity, supervision_mode=mode)
        proposal = dict(
            schema_version="go2_chat_proposal.v1",
            proposal_id=identity,
            plan=asdict(plan),
            scenario=scenario,
            simulate_recipient=True,
            terminal_hold_sim_s=5.0,
            execution_target=TARGET,
            input_sha256=input_hashes,
            supervisor=agent_config,
            supervision_envelope=supervision_envelope() if mode == "agent" else None,
        )
        binding = _digest(proposal)
        task = self.store.create(
            task_id=identity,
            kind=KIND,
            title="Go2・会議室Aへの配送",
            status="proposed",
            owner_session_id=session_id,
            artifacts={"go2_delivery_proposal": proposal, "go2_proposal_sha256": binding},
            metadata={"execution_target": TARGET, "physical_execution_invoked": False},
        )
        self.store.append_event(
            identity, event_type="go2_plan_proposed", payload={"proposal_sha256": binding}
        )
        return task

    def task(self, context, session_id):
        identity = context.get("go2_delivery_task_id")
        task = self.store.get(str(identity or ""))
        if (
            not task
            or task["kind"] != KIND
            or task["owner_session_id"] != session_id
            or context.get("go2_proposal_sha256") != task["artifacts"].get("go2_proposal_sha256")
        ):
            raise ValueError(
                "この会話に結び付いた配送計画を確認できません。新しく配送を依頼してください。"
            )
        return task

    def approve(self, task, session_id):
        if task["status"] == "approved":
            return task
        if task["status"] != "proposed":
            raise ValueError("この配送計画は承認待ちではありません。")
        proposal = task["artifacts"]["go2_delivery_proposal"]
        if _digest(proposal) != task["artifacts"]["go2_proposal_sha256"]:
            raise ValueError("配送計画が変化しました。新しく依頼してください。")
        approval = dict(
            operator_approval_ref=f"go2-chat:{uuid.uuid4().hex}",
            approved_proposal_sha256=_digest(proposal),
            actor_session_id=session_id,
            approved_at=datetime.now(timezone.utc).isoformat(),
        )
        task = self.store.update(
            task["task_id"], status="approved", artifacts={"go2_delivery_approval": approval}
        )
        self.store.append_event(
            task["task_id"], event_type="go2_operator_approved", payload=approval
        )
        return task

    def execute(self, task):
        if task["status"] in ACTIVE or task["status"] == "completed":
            return task
        if task["status"] != "approved":
            raise ValueError("配送計画を承認してから開始してください。")
        if os.environ.get("RUN_MISSIONOS_GO2_DELIVERY_SIM") != "1":
            raise ValueError("このGatewayではGo2シミュレータの実行が有効になっていません。")
        if self.active:
            raise ValueError("別のGo2配送を実行中です。その配送が終了してから開始してください。")
        proposal = task["artifacts"]["go2_delivery_proposal"]
        approval = task["artifacts"].get("go2_delivery_approval", {})
        if (
            approval.get("approved_proposal_sha256") != _digest(proposal)
            or approval.get("actor_session_id") != task["owner_session_id"]
            or self.input_hashes() != proposal["input_sha256"]
        ):
            raise ValueError(
                "承認した配送計画または歩行モデルが変化しました。新しく依頼してください。"
            )
        paths = self.inputs()
        if proposal.get("supervisor"):
            from src.intelligence.go2_supervisor import configuration

            if configuration() != proposal["supervisor"]:
                raise ValueError("承認後に管制Agentの設定が変わりました。再度計画してください。")
            if proposal.get("supervision_envelope") != supervision_envelope():
                raise ValueError("承認後に管制の許可範囲が変わりました。再度計画してください。")
        identity = task["task_id"]
        folder = self.outputs / identity
        self.outputs.mkdir(parents=True, exist_ok=True)
        if folder.exists():
            raise ValueError("既存の実行記録があります。重複して起動しません。")
        manifest = self.outputs / f"{identity}.approved.json"
        manifest.write_text(
            json.dumps(dict(proposal=proposal, approval=approval), ensure_ascii=False)
        )
        task = self.store.update(
            identity, status="starting", artifacts={"go2_output_directory": str(folder)}
        )
        self.active = identity
        self.store.append_event(
            identity,
            event_type="go2_dispatch_reserved",
            payload={"proposal_sha256": _digest(proposal)},
        )
        thread = threading.Thread(
            target=self._worker, args=(identity, paths, manifest), daemon=True
        )
        thread.start()
        return task

    def _worker(self, identity, paths, manifest):
        folder = self.outputs / identity
        proc = None
        try:
            # Simulator processes receive neither model API keys nor Gateway credentials.
            env = {
                key: value
                for key, value in os.environ.items()
                if key
                in {
                    "PATH",
                    "HOME",
                    "USER",
                    "TMPDIR",
                    "LANG",
                    "LC_ALL",
                    "DISPLAY",
                    "MUJOCO_GL",
                    "OMP_NUM_THREADS",
                    "VECLIB_MAXIMUM_THREADS",
                    "IMAGEIO_FFMPEG_EXE",
                    "RUN_MISSIONOS_GO2_DELIVERY_SIM",
                }
            }
            env["PYTHONPATH"] = f"{self.root}:{self.root / 'packages/missionos-core/src'}"
            args = [
                str(paths["python"]),
                str(self.root / "scripts/run_go2_delivery.py"),
                "--model",
                str(paths["model"]),
                "--policy",
                str(paths["policy"]),
                "--output",
                str(folder),
                "--approved-manifest",
                str(manifest),
                "--video",
                "--progress-every-s",
                "1",
            ]
            proc = subprocess.Popen(
                args,
                cwd=self.root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            with self.lock:
                self.workers[identity] = proc
                current = self.store.get(identity)
                if current["status"] == "cancel_requested":
                    (self.outputs / f"{identity}.cancel").touch()
                else:
                    self.store.update(identity, status="running")
            trail = []
            with (self.outputs / f"{identity}.log").open("w") as log:
                for line in proc.stdout:
                    log.write(line)
                    log.flush()
                    try:
                        event = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    if "xy" in event:
                        trail.append(event["xy"])
                        self.store.update(
                            identity,
                            artifacts={
                                "go2_delivery_snapshot": event,
                                "go2_trajectory": trail[-1000:],
                            },
                        )
                    if event.get("event"):
                        self.store.append_event(identity, event_type=event["event"], payload=event)
                        if event["event"] == "go2_supervision_requested":
                            self._judge_request(identity, event["request"])
                        if event["event"] == "go2_supervision_decided":
                            task = self.store.get(identity)
                            decisions = task["artifacts"].get("go2_supervision_decisions", [])
                            self.store.update(
                                identity,
                                artifacts={
                                    "go2_supervision_decisions": decisions + [event["decision"]]
                                },
                            )
                        if event["event"].startswith("dynamic_"):
                            self.store.update(identity, artifacts={"go2_local_avoidance": event})
                        phase = {
                            "recipient_confirmation_required": "awaiting_receipt",
                            "parcel_receipt_observed": "returning",
                            "bounded_retry_selected": "waiting_to_retry",
                            "terminal_hold_started": "verifying_return",
                        }.get(event["event"])
                        if phase:
                            self.store.update(identity, artifacts={"go2_delivery_phase": phase})
                        if event["event"] == "parcel_receipt_observed":
                            self.store.update(
                                identity, artifacts={"go2_simulated_receipt_observed": True}
                            )
            code = proc.wait(timeout=5)
            result = json.loads((folder / "result.json").read_text())
            proposal = self.store.get(identity)["artifacts"]["go2_delivery_proposal"]
            if (
                result.get("mission_id") != identity
                or result.get("approved_proposal_sha256") != _digest(proposal)
                or result.get("plan_sha256") != Go2DeliveryPlan(**proposal["plan"]).digest
                or result.get("physical_execution_invoked") is not False
            ):
                raise ValueError("Simulator returned an unbound result")
            status = result.get("status")
            if status == "completed" and not (
                code == 0
                and result.get("completion_claimed") is True
                and result.get("receipt", {}).get("source") == "simulation_recipient"
                and any(
                    e.get("event") == "terminal_hold_verified" for e in result.get("events", [])
                )
            ):
                raise ValueError("Completion evidence is incomplete")
            if status == "returned_undelivered" and not (
                result.get("completion_claimed") is False
                and not result.get("receipt")
                and any(
                    e.get("event") == "undelivered_return_verified"
                    for e in result.get("events", [])
                )
            ):
                raise ValueError("Undelivered return evidence is incomplete")
            if status not in (
                "completed",
                "needs_attention",
                "canceled",
                "awaiting_receipt",
                "returned_undelivered",
            ):
                status = "needs_attention"
            terminal = result.get("terminal_state", {})
            self.store.update(
                identity,
                status=status,
                artifacts={
                    "go2_delivery_result": result,
                    "go2_delivery_snapshot": {
                        "phase": PHASES.get(status, status),
                        "xy": terminal.get("ground_truth_xy"),
                        "sim_time_s": terminal.get("sim_time_s"),
                        "measured_speed_mps": terminal.get("measured_speed_mps"),
                    },
                },
                ended_at=time.time(),
            )
            self.store.append_event(
                identity,
                event_type="go2_runtime_finished",
                payload={"exit_code": code, "status": status},
            )
        except Exception as exc:
            self.store.update(
                identity, status="needs_attention", error=str(exc), ended_at=time.time()
            )
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            with self.lock:
                self.workers.pop(identity, None)
                if self.active == identity:
                    self.active = None

    def _judge_request(self, identity, request):
        from src.intelligence.go2_supervisor import judge

        with self.lock:
            task = self.store.get(identity)
            proposal = task["artifacts"]["go2_delivery_proposal"]
            count = task["artifacts"].get("go2_supervision_request_count", 0)
            expected_id = f"decision_{count + 1}"
            if (
                not proposal.get("supervisor")
                or task["status"] not in ACTIVE
                or self.active != identity
                or count >= MAX_DECISIONS
                or request.get("observation_id") != expected_id
                or request.get("mission_id") != identity
                or request.get("approved_plan_sha256") != Go2DeliveryPlan(**proposal["plan"]).digest
                or task["artifacts"]["go2_delivery_approval"]["approved_proposal_sha256"]
                != _digest(proposal)
            ):
                raise ValueError("Unbound or over-budget supervision request")
            self.store.update(identity, artifacts={"go2_supervision_request_count": count + 1})
        try:
            if task["status"] == "cancel_requested":
                response = {
                    "request_sha256": digest(request),
                    "judge_status": "canceled",
                    "proposal": {},
                }
            else:
                response = judge(request, proposal["supervisor"])
        except Exception as exc:
            response = {
                "request_sha256": digest(request),
                "judge_status": "unavailable",
                "proposal": {},
                "error_type": type(exc).__name__,
            }
        folder = self.outputs / identity / "supervision"
        temporary = folder / f"{expected_id}.response.tmp"
        temporary.write_text(json.dumps(response, ensure_ascii=False))
        temporary.replace(folder / f"{expected_id}.response.json")
        self.store.append_event(
            identity,
            event_type="go2_supervisor_response_received",
            payload={
                "observation_id": expected_id,
                "judge_status": response["judge_status"],
                "invocation": response.get("invocation", {}),
            },
        )

    def cancel(self, task):
        if task["status"] in ("proposed", "approved"):
            return self.store.update(task["task_id"], status="rejected")
        if task["status"] in ACTIVE:
            (self.outputs / f"{task['task_id']}.cancel").touch()
            self.store.append_event(task["task_id"], event_type="go2_cancel_requested")
            return self.store.update(task["task_id"], status="cancel_requested")
        return task

    def response(self, task, context, intent, message=""):
        result = task["artifacts"].get("go2_delivery_result", {})
        snap = task["artifacts"].get("go2_delivery_snapshot", {})
        phase = (
            task["status"]
            if task["status"] not in {"starting", "running"}
            else snap.get("phase", task["status"])
        )
        summary = dict(
            task_id=task["task_id"],
            status=task["status"],
            execution_target=TARGET,
            phase=PHASES.get(phase, phase),
            completion_claimed=result.get("completion_claimed") is True,
            physical_execution_invoked=False,
            llm_judgment_invoked=result.get("llm_judgment_invoked", False)
            or any(
                d.get("invocation", {}).get("response_sha256")
                not in (None, sha256(b"").hexdigest())
                for d in task["artifacts"].get("go2_supervision_decisions", [])
            ),
            supervision_mode=task["artifacts"]["go2_delivery_proposal"]["plan"].get(
                "supervision_mode", "rules"
            ),
            judgments=[
                {
                    "proposal": d.get("proposal", {}),
                    "blocking_reasons": d.get("rule_blocking_reasons", []),
                    "model_id": d.get("invocation", {}).get("model_id"),
                    "observation_id": d.get("observation", {}).get("observation_id"),
                }
                for d in task["artifacts"].get("go2_supervision_decisions", [])
            ],
            local_avoidance=task["artifacts"].get("go2_local_avoidance", {}),
            dynamic_avoidance=result.get("dynamic_avoidance", {}),
            snapshot=snap,
            trajectory=task["artifacts"].get("go2_trajectory", []),
            receipt=result.get("receipt"),
            simulated_receipt_observed=task["artifacts"].get("go2_simulated_receipt_observed")
            is True,
            error=task.get("error"),
        )
        context = dict(context, summary={**context.get("summary", {}), **summary})
        if not message and intent == "status" and summary["judgments"]:
            latest = summary["judgments"][-1]
            message = (
                summary["phase"]
                + "\nAgentの判断："
                + latest["proposal"].get("rationale", "判断を確認できません")
            )
        return dict(
            schema_version="missionos_autonomy_conversation_response.v1",
            routed_action=intent,
            routing_source="go2_delivery_fixed_catalog",
            message=message or summary["phase"],
            mission_designer=context,
            operation_result=context,
            progress_counted=False,
            conversation_route_bypassed_guardrails=False,
            missionos_agent_invocations=[
                d["invocation"]
                for d in task["artifacts"].get("go2_supervision_decisions", [])
                if d.get("invocation")
            ],
            llm_judgment_invoked=summary["llm_judgment_invoked"],
        )

    def handle(self, request, text, session_id, context, register: Callable):
        if re.search(OTHER_ROBOTS, text, re.I):
            return None
        go2_context = str(context.get("mission_designer_context_ref", "")).startswith(
            "mission_designer_context:go2_"
        )
        if not (requested(text) or context.get("go2_delivery_task_id") or go2_context):
            return None
        intent = COMMANDS.get(text, "status")
        try:
            if not session_id:
                raise ValueError("会話IDが必要です。通常のMissionOSチャットから依頼してください。")
            if (
                go2_context
                and context.get("mission_designer_context_error")
                and not requested(text)
            ):
                raise ValueError(
                    "配送計画の承認情報を確認できません。新しく配送を依頼してください。"
                )
            with self.lock:
                if requested(text):
                    task = self.plan(
                        text,
                        session_id,
                        request.get("go2_scenario", "baseline"),
                        request.get("go2_supervision_mode"),
                    )
                    proposal = task["artifacts"]["go2_delivery_proposal"]
                    context = register(
                        dict(
                            scenario_proposal=proposal,
                            validation_result={"valid": True},
                            go2_delivery_task_id=task["task_id"],
                            go2_proposal_sha256=task["artifacts"]["go2_proposal_sha256"],
                        ),
                        session_id=session_id,
                    )
                    return self.response(
                        task,
                        context,
                        "mission_designer_plan",
                        "Go2で受付から会議室Aへ配送し、模擬受領を確認して受付へ戻ります。"
                        "通路が塞がった場合は、行き・帰りそれぞれ最大1回だけ再計画します。屋内シミュレーションです。"
                        + (
                            " Agentは最大3回判断し、合計20秒までの待機・同じ配送先への迂回・未配送での受付帰還を選べます。範囲外は停止して確認します。"
                            if proposal["plan"]["supervision_mode"] == "agent"
                            else ""
                        )
                        + " /approve で計画を承認し、/run で開始できます。",
                    )
                task = self.task(context, session_id)
                if intent in ("approve", "approve_and_run"):
                    task = self.approve(task, session_id)
                if intent in ("execute", "approve_and_run"):
                    task = self.execute(task)
                if intent in ("reject", "cancel"):
                    task = self.cancel(task)
                message = (
                    "配送計画を承認しました。/run で開始できます。" if intent == "approve" else ""
                )
                return self.response(
                    task, context, "execute" if intent == "approve_and_run" else intent, message
                )
        except (ValueError, OSError) as exc:
            return dict(
                schema_version="missionos_autonomy_conversation_response.v1",
                routed_action="clarification",
                routing_source="go2_delivery_fixed_catalog",
                message=str(exc),
                mission_designer=context,
                operation_result={"error": str(exc)},
                progress_counted=False,
                conversation_route_bypassed_guardrails=False,
            )

    def close(self):
        with self.lock:
            workers = list(self.workers.items())
            if self.active:
                (self.outputs / f"{self.active}.cancel").touch()
            for identity, _ in workers:
                (self.outputs / f"{identity}.cancel").touch()
        for identity, proc in workers:
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                self.store.update(
                    identity,
                    status="needs_attention",
                    error="Gateway shutdown interrupted the simulator.",
                )


_services: dict[str, Go2ChatService] = {}
_service_lock = threading.Lock()


def service():
    store = get_task_store()
    key = str(store.db_path.resolve())
    with _service_lock:
        if key not in _services:
            _services[key] = Go2ChatService(store)
        return _services[key]
