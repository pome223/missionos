"""Yokohama harbour drone delivery through the normal MissionOS conversation route.

DeepSeek interprets the chat request against a one-route catalog and, during
the flight, may only add a bounded wait to a Rules pad entry. Server-owned
context and task records bind explicit operator approval to the exact plan and
simulator inputs. The worker runs the opt-in PX4/Gazebo simulator in its own
Python environment without model API keys. The task is reported complete only
when the run passes and every verifier passes. No hardware is used.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from typing import Callable
import uuid

from src.gateway.go2_delivery_chat import COMMANDS
from src.runtime.task_store import TaskStore, get_task_store
from src.runtime.yokohama_payload import digest


KIND = "yokohama_delivery_execution"
TARGET = "px4_gazebo_yokohama_harbour_delivery"
ACTIVE = {"starting", "running", "cancel_requested"}
OTHER_ROBOTS = r"(go2|unitree|turtlebot|タートルボット|nova[ -]?carter|gr00t|会議室)"
ROUTE = dict(
    route_id="yokohama_ship_to_harbour_pad_v1",
    stages=[
        "船から離陸し、海上約1 kmを横浜へ",
        "街中の判断地点D1・D2で建物の間を通過",
        "配送パッド手前で待機（先行機が使用中なら待つ）",
        "パッドで荷物（50 g）を届ける",
        "同じ経路で船へ戻り着陸",
    ],
    world="Yokohama urban 3D scene with harbour, ship, delivery pad and a scripted lead aircraft",
)
# Sources whose change after approval invalidates the plan.
SOURCES = (
    "scripts/yokohama_sitl.py",
    "scripts/yokohama_pad_worker.py",
    "scripts/yokohama_flight_worker.py",
    "scripts/yokohama_decision_host.py",
    "scripts/yokohama_pad_advisory_host.py",
    "src/runtime/yokohama_pad_queue.py",
    "src/runtime/yokohama_pad_advisory_contract.py",
    "src/runtime/yokohama_native.py",
    "docs/examples/yokohama-urban-scene/files.sha256.json",
    "docs/examples/yokohama-pad-state/model/model.json",
)
VERIFIERS = ("decisions", "pad_queue", "pad_advisory", "payload", "sitl")
STAGES = {
    "SEA-TAKEOFF": "船から離陸",
    "SEA-INBOUND-COAST": "海上を横浜へ飛行中",
    "00-D1": "街中の判断地点D1",
    "01-D2": "街中の判断地点D2",
    "02-D3": "配送パッド手前の待機地点へ",
    "03-DELIVERY": "パッドへ進入",
    "PAYLOAD-LOW": "荷物を降ろしています",
    "PAYLOAD-CLIMB": "荷物を届けて上昇",
    "04-D3": "街中を戻っています",
    "05-D2": "街中を戻っています",
    "06-D1": "街中を戻っています",
    "SEA-OUTBOUND-COAST": "海上を船へ飛行中",
    "SEA-RETURN": "船へ戻っています",
}
MILESTONES = {
    "city_segment_arrived": "街中の判断地点に到着",
    "city_session_revoked": "街中のモデルを停止",
    "pad_occupied_reported": "先行機がパッドを使用中のため待機",
    "pad_entry_authorized": "パッドへの進入をルールが許可",
    "payload_received": "荷物の受け取りを確認",
    "landing_observed": "船に着陸",
    "missionos_response_timeout": "MissionOSの応答がなく停止",
}
PHASES = {
    "proposed": "配送計画の承認待ち",
    "approved": "配送承認済み",
    "starting": "シミュレータを準備中",
    "running": "飛行中",
    "cancel_requested": "停止を確認中",
    "completed": "配送・帰船と全検証の合格を確認",
    "needs_attention": "停止・対応が必要",
    "rejected": "配送計画を却下",
    "canceled": "配送を中止",
    "superseded": "新しい計画に置き換え",
}
SIM_ENV = {
    "PATH",
    "HOME",
    "USER",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "DOCKER_HOST",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
}


def requested(text: str) -> bool:
    return bool(
        not re.search(OTHER_ROBOTS, text, re.I)
        and re.search(r"(横浜|yokohama)", text, re.I)
        and re.search(r"(配送|配達|届け|deliver)", text, re.I)
    )


def _digest(value: dict) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _atomic(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False))
    temporary.replace(path)


class YokohamaChatService:
    def __init__(self, store: TaskStore):
        self.store = store
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.worker: threading.Thread | None = None
        self.active: str | None = None
        self.process: subprocess.Popen | None = None
        self.interrupted_process: subprocess.Popen | None = None
        self.verification_process: subprocess.Popen | None = None
        self.root = Path(__file__).resolve().parents[2]
        self.outputs = Path(
            os.environ.get("MISSIONOS_YOKOHAMA_OUTPUT_ROOT", "output/yokohama-chat")
        ).resolve()
        tasks, page = [], 1
        while True:
            result = self.store.query(kind=KIND, page=page, page_size=100)
            tasks.extend(result["tasks"])
            if not result["pagination"]["has_more"]:
                break
            page += 1
        for task in tasks:
            if task["status"] in ACTIVE:
                self.store.update(
                    task["task_id"],
                    status="needs_attention",
                    error="Gatewayが再起動したため、前回の飛行の結果は未確認です。新しく依頼してください。",
                )

    def inputs(self):
        python = Path(os.environ.get("MISSIONOS_YOKOHAMA_SITL_PYTHON", ""))
        if not python.name or not python.is_file():
            raise ValueError(
                "横浜配送のシミュレータ環境が未設定です（MISSIONOS_YOKOHAMA_SITL_PYTHON）。"
            )
        models = os.environ.get("MISSIONOS_YOKOHAMA_CITY_MODELS", "fixture")
        if models not in ("fixture", "native"):
            raise ValueError("未対応の街中モデル設定です。")
        service = None
        if models == "native":
            service = Path(os.environ.get("MISSIONOS_YOKOHAMA_NATIVE_SERVICE_CONFIG", ""))
            if not service.name or not service.is_file():
                raise ValueError("実VLA・WAMの接続設定がありません。")
        return dict(python=python, city_models=models, service=service)

    def input_hashes(self, inputs):
        hashes = {name: sha256((self.root / name).read_bytes()).hexdigest() for name in SOURCES}
        if inputs["service"]:
            hashes["native_service_config"] = sha256(inputs["service"].read_bytes()).hexdigest()
        return hashes

    @staticmethod
    def arguments(inputs):
        args = [
            "--phase",
            "flight",
            "--sea-round-trip",
            "--deliver-payload",
            "--occupied-pad",
            "--pad-state-advisory",
            "assist",
            "--pad-mission-judge",
            "gateway",
            "--decision-backend",
            inputs["city_models"],
            "--wam-profile",
            "motion-v4",
            "--timeout-seconds",
            "3000",
        ]
        if inputs["service"]:
            args += ["--native-service-config", str(inputs["service"])]
        return args

    def plan(self, text, session_id):
        from src.intelligence import yokohama_delivery_agents as agents

        agent_config = agents.configuration()
        inputs = self.inputs()
        reading = agents.plan(text, agent_config)
        output = reading["output"]
        if not output["supported"]:
            raise ValueError(output["reason"])
        for prior in self.store.list(kind=KIND, owner_session_id=session_id, limit=100):
            if prior["status"] in ACTIVE:
                raise ValueError("この会話の配送を実行中です。状況確認または中止を選んでください。")
            if prior["status"] in ("proposed", "approved"):
                self.store.update(prior["task_id"], status="superseded")
        identity = f"yokohama_{uuid.uuid4().hex[:16]}"
        invocation = reading["invocation"]
        proposal = dict(
            schema_version="yokohama_chat_proposal.v1",
            proposal_id=identity,
            route=ROUTE,
            destination_id=output["destination_id"],
            city_models=inputs["city_models"],
            pad_queue=dict(
                decider="pose Rules with a 5 s clear window",
                advisory="CPU pad camera forecast, assist mode (may only add waiting)",
                mission_judge=dict(
                    agent=agent_config["judge"],
                    may_only_add_wait=True,
                    max_decisions=2,
                    max_added_wait_s=30,
                ),
            ),
            simulator_arguments=self.arguments(inputs),
            input_sha256=self.input_hashes(inputs),
            agents=agent_config,
            planner=dict(
                summary=output["summary"],
                model_id=invocation.get("model_id"),
                prompt_sha256=invocation.get("prompt_sha256"),
                response_sha256=invocation.get("response_sha256"),
            ),
            execution_target=TARGET,
            physical_execution_invoked=False,
        )
        binding = _digest(proposal)
        task = self.store.create(
            task_id=identity,
            kind=KIND,
            title="横浜・船から配送パッドへの配送",
            status="proposed",
            owner_session_id=session_id,
            artifacts={"yokohama_delivery_proposal": proposal, "yokohama_proposal_sha256": binding},
            metadata={"execution_target": TARGET, "physical_execution_invoked": False},
        )
        self.store.append_event(
            identity,
            event_type="yokohama_plan_proposed",
            payload={"proposal_sha256": binding, "planner_model_id": invocation.get("model_id")},
        )
        return task

    def task(self, context, session_id):
        task = self.store.get(str(context.get("yokohama_delivery_task_id") or ""))
        if (
            not task
            or task["kind"] != KIND
            or task["owner_session_id"] != session_id
            or context.get("yokohama_proposal_sha256")
            != task["artifacts"].get("yokohama_proposal_sha256")
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
        proposal = task["artifacts"]["yokohama_delivery_proposal"]
        if _digest(proposal) != task["artifacts"]["yokohama_proposal_sha256"]:
            raise ValueError("配送計画が変化しました。新しく依頼してください。")
        approval = dict(
            operator_approval_ref=f"yokohama-chat:{uuid.uuid4().hex}",
            approved_proposal_sha256=_digest(proposal),
            actor_session_id=session_id,
            approved_at=datetime.now(timezone.utc).isoformat(),
        )
        task = self.store.update(
            task["task_id"], status="approved", artifacts={"yokohama_delivery_approval": approval}
        )
        self.store.append_event(
            task["task_id"], event_type="yokohama_operator_approved", payload=approval
        )
        return task

    def running_simulators(self):
        try:
            run = subprocess.run(
                ["docker", "ps", "--filter", "name=missionos-yokohama-", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
                timeout=20,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("Dockerの状態を確認できません。") from exc
        return run.stdout.split()

    def execute(self, task):
        with self.lock:
            if task["status"] in ACTIVE or task["status"] == "completed":
                return task
            if task["status"] != "approved":
                raise ValueError("配送計画を承認してから開始してください。")
            if os.environ.get("RUN_MISSIONOS_YOKOHAMA_DELIVERY_SIM") != "1":
                raise ValueError(
                    "このGatewayでは横浜配送シミュレータの実行が有効になっていません。"
                )
            if self.stopping.is_set():
                raise ValueError("Gatewayは停止中です。")
            if self.active:
                raise ValueError(
                    "別の横浜配送を実行中です。その配送が終了してから開始してください。"
                )
            from src.intelligence import yokohama_delivery_agents as agents

            proposal = task["artifacts"]["yokohama_delivery_proposal"]
            approval = task["artifacts"].get("yokohama_delivery_approval", {})
            inputs = self.inputs()
            if (
                approval.get("approved_proposal_sha256") != _digest(proposal)
                or approval.get("actor_session_id") != task["owner_session_id"]
                or self.input_hashes(inputs) != proposal["input_sha256"]
                or self.arguments(inputs) != proposal["simulator_arguments"]
                or agents.configuration() != proposal["agents"]
            ):
                raise ValueError(
                    "承認した配送計画・シミュレータ入力・Agent設定のいずれかが変化しました。"
                    "新しく依頼してください。"
                )
            if self.running_simulators():
                raise ValueError(
                    "別の横浜シミュレータが動作中です。終了を確認してから開始してください。"
                )
            identity = task["task_id"]
            base = self.outputs / identity
            base.mkdir(parents=True, exist_ok=False)
            manifest = base / "approved.json"
            manifest.write_text(
                json.dumps(dict(proposal=proposal, approval=approval), ensure_ascii=False)
            )
            folder = base / "run"
            task = self.store.update(
                identity, status="starting", artifacts={"yokohama_output_directory": str(folder)}
            )
            self.active = identity
            self.store.append_event(
                identity,
                event_type="yokohama_dispatch_reserved",
                payload={"proposal_sha256": _digest(proposal)},
            )
            self.worker = threading.Thread(
                target=self._worker, args=(identity, inputs, folder, manifest), daemon=True
            )
            self.worker.start()
            return task

    def _interrupt(self, proc):
        # Cancellation and shutdown can overlap; interrupt owned cleanup only once.
        with self.lock:
            if self.interrupted_process is not proc and proc.poll() is None:
                self.interrupted_process = proc
                try:
                    proc.send_signal(signal.SIGINT)
                except ProcessLookupError:
                    pass

    @staticmethod
    def _kill_group(proc):
        # Every owned child starts a new session. Reap descendants even if its
        # leader has exited; a killed parent alone does not end inherited work.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _reap(self, proc, grace=180):
        try:
            if proc.poll() is None:
                self._interrupt(proc)
                try:
                    proc.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    self._kill_group(proc)
                    proc.wait()
            else:
                proc.wait()
        finally:
            self._kill_group(proc)

    def _verify(self, args, env, identity):
        proc = None
        try:
            with self.lock:
                if (
                    self.stopping.is_set()
                    or self.store.get(identity)["status"] == "cancel_requested"
                ):
                    return
            proc = subprocess.Popen(
                args,
                cwd=self.root,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            with self.lock:
                self.verification_process = proc
                if (
                    self.stopping.is_set()
                    or self.store.get(identity)["status"] == "cancel_requested"
                ):
                    self._interrupt(proc)
            deadline = time.monotonic() + 1800
            while proc.poll() is None:
                if (
                    self.stopping.is_set()
                    or self.store.get(identity)["status"] == "cancel_requested"
                ):
                    break
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(args, 1800)
                self.stopping.wait(0.05)
        finally:
            if proc:
                self._reap(proc, grace=1)
            with self.lock:
                self.verification_process = None

    def _worker(self, identity, inputs, folder, manifest):
        proc = None
        reaped = False
        proposal = self.store.get(identity)["artifacts"]["yokohama_delivery_proposal"]
        try:
            if self.stopping.is_set():
                return
            # The simulator receives neither model API keys nor Gateway credentials.
            env = {key: value for key, value in os.environ.items() if key in SIM_ENV}
            args = [
                str(inputs["python"]),
                str(self.root / "scripts/yokohama_sitl.py"),
                *proposal["simulator_arguments"],
                "--approve-sitl",
                "--approval-manifest",
                str(manifest),
                "--output-dir",
                str(folder),
            ]
            with (manifest.parent / "simulator.log").open("w") as log:
                proc = subprocess.Popen(
                    args,
                    cwd=self.root,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                with self.lock:
                    self.process = proc
                    self.store.update(identity, artifacts={"yokohama_worker_pid": proc.pid})
                    if (
                        self.stopping.is_set()
                        or self.store.get(identity)["status"] == "cancel_requested"
                    ):
                        self._interrupt(proc)
                    else:
                        self.store.update(identity, status="running")
                offset, judged = 0, set()
                while (
                    proc.poll() is None
                    and not self.stopping.is_set()
                    and self.store.get(identity)["status"] != "cancel_requested"
                ):
                    offset = self._follow(identity, folder / "flight-events.jsonl", offset)
                    self._judge_pending(identity, folder, proposal, judged)
                    time.sleep(0.5)
                self._follow(identity, folder / "flight-events.jsonl", offset)
            self._reap(proc)
            reaped = True
            if not self.stopping.is_set():
                self._finish(identity, folder, inputs, proc.returncode)
        except Exception as exc:  # noqa: BLE001 - every failure leaves the task for review.
            with self.lock:
                if not self.stopping.is_set():
                    canceled = self.store.get(identity)["status"] == "cancel_requested"
                    self.store.update(
                        identity,
                        status="canceled" if canceled else "needs_attention",
                        error=str(exc),
                        ended_at=time.time(),
                    )
        finally:
            if proc and not reaped:
                self._reap(proc)
            with self.lock:
                self.process = None
                if self.active == identity:
                    self.active = None

    def _follow(self, identity, path, offset):
        if not path.is_file():
            return offset
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read()
        end = data.rfind(b"\n") + 1
        for line in data[:end].splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            name, phase = event.get("event"), event.get("phase")
            if name == "hold_started" and phase in STAGES:
                self.store.update(identity, artifacts={"yokohama_phase": STAGES[phase]})
            if name in MILESTONES:
                label = MILESTONES[name]
                self.store.append_event(
                    identity,
                    event_type=f"yokohama_{name}",
                    payload=dict(label=label, phase=phase, wall_s=event.get("wall_s")),
                )
                self.store.update(identity, artifacts={"yokohama_phase": label})
        return offset + end

    def _judge_pending(self, identity, folder, proposal, judged):
        from src.intelligence import yokohama_delivery_agents as agents

        root = folder / "pad-judge"
        if not root.is_dir():
            return
        for request_path in sorted(root.glob("*/request.json")):
            if request_path.parent in judged or (request_path.parent / "response.json").exists():
                continue
            judged.add(request_path.parent)
            request = json.loads(request_path.read_text())
            content = {k: v for k, v in request.items() if k != "judge_request_id"}
            if digest(content) != request.get("judge_request_id"):
                raise ValueError("Unbound pad judge request")
            answer = agents.judge(request, proposal["agents"])
            _atomic(request_path.parent / "response.json", answer)
            decision = answer.get("decision", {})
            record = dict(
                observation_id=request["observation_id"],
                judge_status=answer["judge_status"],
                action=decision.get("action"),
                wait_seconds=decision.get("wait_seconds"),
                rationale=decision.get("rationale"),
                model_id=answer.get("invocation", {}).get("model_id"),
                response_sha256=answer.get("invocation", {}).get("response_sha256"),
            )
            task = self.store.get(identity)
            decisions = task["artifacts"].get("yokohama_judge_decisions", [])
            self.store.update(
                identity, artifacts={"yokohama_judge_decisions": decisions + [record]}
            )
            self.store.append_event(
                identity, event_type="yokohama_pad_judge_answered", payload=record
            )

    def _finish(self, identity, folder, inputs, code):
        result_path = folder / "result.json"
        result = json.loads(result_path.read_text()) if result_path.is_file() else {}
        verification = {}
        if result.get("status") == "passed":
            env = {key: value for key, value in os.environ.items() if key in SIM_ENV}
            for name in VERIFIERS:
                if self.stopping.is_set():
                    return
                output = folder / f"verification-{name}.json"
                self._verify(
                    [
                        str(inputs["python"]),
                        str(self.root / f"scripts/verify_yokohama_{name}.py"),
                        str(folder),
                        "--output",
                        str(output),
                    ],
                    env,
                    identity,
                )
                verification[name] = (
                    json.loads(output.read_text()).get("status") if output.is_file() else "error"
                )
        passed = (
            code == 0
            and result.get("status") == "passed"
            and len(verification) == len(VERIFIERS)
            and all(status == "passed" for status in verification.values())
        )
        with self.lock:
            if self.stopping.is_set():
                return
            canceled = self.store.get(identity)["status"] == "cancel_requested"
            status = "canceled" if canceled else "completed" if passed else "needs_attention"
            self.store.update(
                identity,
                status=status,
                artifacts={
                    "yokohama_result": {
                        k: result.get(k)
                        for k in ("run_id", "status", "reason", "physical_execution_invoked")
                    },
                    "yokohama_verification": verification,
                    "yokohama_trajectory": self._trajectory(folder),
                    "yokohama_phase": PHASES[status],
                },
                error=None
                if passed
                else result.get("reason") or "飛行または検証が合格しませんでした。",
                ended_at=time.time(),
            )
            self.store.append_event(
                identity,
                event_type="yokohama_runtime_finished",
                payload=dict(exit_code=code, status=status, verification=verification),
            )

    @staticmethod
    def _trajectory(folder, points=300):
        path = folder / "flight-trajectory.jsonl"
        if not path.is_file():
            return []
        rows = path.read_text().splitlines()
        step = max(1, len(rows) // points)
        track = []
        for line in rows[::step]:
            xyz = json.loads(line).get("vehicle", {}).get("xyz")
            if xyz:
                track.append([round(v, 1) for v in xyz])
        return track

    def cancel(self, task):
        with self.lock:
            task = self.store.get(task["task_id"])
            if task["status"] in ("proposed", "approved"):
                return self.store.update(task["task_id"], status="rejected")
            if task["status"] in ACTIVE:
                self.store.append_event(task["task_id"], event_type="yokohama_cancel_requested")
                task = self.store.update(task["task_id"], status="cancel_requested")
                if self.active == task["task_id"]:
                    for proc in (self.process, self.verification_process):
                        if proc:
                            self._interrupt(proc)
            return task

    def response(self, task, context, intent, message=""):
        artifacts = task["artifacts"]
        judgments = artifacts.get("yokohama_judge_decisions", [])
        summary = dict(
            task_id=task["task_id"],
            status=task["status"],
            execution_target=TARGET,
            phase=(
                artifacts.get("yokohama_phase", PHASES.get(task["status"], task["status"]))
                if task["status"] in ("running", "cancel_requested")
                else PHASES.get(task["status"], task["status"])
            ),
            city_models=artifacts["yokohama_delivery_proposal"]["city_models"],
            judgments=judgments,
            verification=artifacts.get("yokohama_verification", {}),
            trajectory=artifacts.get("yokohama_trajectory", []),
            physical_execution_invoked=False,
            llm_judgment_invoked=any(j.get("judge_status") == "valid" for j in judgments),
            error=task.get("error"),
        )
        context = dict(context, summary={**context.get("summary", {}), **summary})
        if not message:
            lines = [summary["phase"]]
            if judgments:
                latest = judgments[-1]
                lines.append(
                    "DeepSeekの判断："
                    + (
                        latest.get("rationale")
                        or f"応答を確認できません（{latest['judge_status']}）"
                    )
                )
            if summary["verification"]:
                lines.append(
                    "検証：" + "、".join(f"{k} {v}" for k, v in summary["verification"].items())
                )
            if task["status"] == "needs_attention" and task.get("error"):
                lines.append(str(task["error"]))
            message = "\n".join(lines)
        return dict(
            schema_version="missionos_autonomy_conversation_response.v1",
            routed_action=intent,
            routing_source="yokohama_delivery_catalog",
            message=message,
            mission_designer=context,
            operation_result=context,
            progress_counted=False,
            conversation_route_bypassed_guardrails=False,
            llm_judgment_invoked=summary["llm_judgment_invoked"],
        )

    def handle(self, request, text, session_id, context, register: Callable):
        yokohama_context = str(context.get("mission_designer_context_ref", "")).startswith(
            "mission_designer_context:yokohama_"
        )
        if not (requested(text) or context.get("yokohama_delivery_task_id") or yokohama_context):
            return None
        intent = COMMANDS.get(text, "status")
        try:
            if not session_id:
                raise ValueError("会話IDが必要です。通常のMissionOSチャットから依頼してください。")
            if (
                yokohama_context
                and context.get("mission_designer_context_error")
                and not requested(text)
            ):
                raise ValueError(
                    "配送計画の承認情報を確認できません。新しく配送を依頼してください。"
                )
            with self.lock:
                if requested(text):
                    task = self.plan(text, session_id)
                    proposal = task["artifacts"]["yokohama_delivery_proposal"]
                    context = register(
                        dict(
                            scenario_proposal=proposal,
                            validation_result={"valid": True},
                            yokohama_delivery_task_id=task["task_id"],
                            yokohama_proposal_sha256=task["artifacts"]["yokohama_proposal_sha256"],
                        ),
                        session_id=session_id,
                    )
                    models = (
                        "実VLA・実WAM"
                        if proposal["city_models"] == "native"
                        else "固定応答（GPUなし）"
                    )
                    return self.response(
                        task,
                        context,
                        "mission_designer_plan",
                        proposal["planner"]["summary"]
                        + f"\n街中の判断地点は{models}で進路を提案・確認し、ルールが制限します。"
                        "パッドの順番待ちはルールが判断し、DeepSeekは最大2回・合計30秒まで"
                        "待ち時間を追加することだけを提案できます。シミュレーションです。"
                        " /approve で計画を承認し、/run で開始できます。",
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
                routing_source="yokohama_delivery_catalog",
                message=str(exc),
                mission_designer=context,
                operation_result={"error": str(exc)},
                progress_counted=False,
                conversation_route_bypassed_guardrails=False,
            )

    def close(self):
        with self.lock:
            self.stopping.set()
            processes = (self.process, self.verification_process)
            identity, worker = self.active, self.worker
            if identity:
                canceled = self.store.get(identity)["status"] in ("cancel_requested", "canceled")
                self.store.update(
                    identity,
                    status="canceled" if canceled else "needs_attention",
                    error="Gatewayの停止によりシミュレータを中断しました。",
                    ended_at=time.time(),
                )
        for proc in processes:
            if proc:
                self._interrupt(proc)
        if worker and worker is not threading.current_thread():
            worker.join()


_services: dict[str, YokohamaChatService] = {}
_service_lock = threading.Lock()


def service():
    store = get_task_store()
    key = str(store.db_path.resolve())
    with _service_lock:
        previous = _services.get(key)
        if previous and previous.stopping.is_set():
            if previous.worker and previous.worker.is_alive():
                raise ValueError("前のGatewayの配送workerが停止中です。")
            del _services[key]
        if key not in _services:
            _services[key] = YokohamaChatService(store)
        return _services[key]
