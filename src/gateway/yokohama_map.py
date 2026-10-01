"""Pre-departure 3D map UI, shared TaskStore approvals and CPU fixture runner."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import subprocess
import sys
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.runtime.task_store import TaskStore
from src.runtime.yokohama_goal_fixture import run
from src.runtime.yokohama_payload import digest

KIND = "yokohama_map_delivery"
ACTIVE = {"starting", "running", "cancel_requested"}
ROOT = Path(__file__).resolve().parents[2]


class MapRequest(BaseModel):
    session_id: str = Field(min_length=16, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    goal_xy_m: list[float] | None = None
    task_id: str | None = None
    plan_sha256: str | None = None


class MapService:
    def __init__(self, store: TaskStore, outputs=None, delay=0.01):
        self.store = store
        from src.runtime.yokohama_goal import GoalPlanner

        self.planner = GoalPlanner()
        self.backend = os.environ.get("MISSIONOS_YOKOHAMA_MAP_BACKEND", "sitl")
        if self.backend not in {"sitl", "fixture"}:
            raise ValueError("Map backend must be sitl or fixture")
        self.process = None
        self.interrupted_process = None
        self.outputs = Path(
            outputs or os.environ.get("MISSIONOS_YOKOHAMA_MAP_OUTPUT_ROOT", "output/yokohama-map")
        ).resolve()
        from src.gateway.yokohama_dispatch import shared_dispatch
        self.dispatch = shared_dispatch(store)
        self.lock = self.dispatch.lock
        self.active = None
        self.cancel_event = threading.Event()
        self.worker = None
        self.delay = delay
        page = 1
        while True:
            rows = store.query(kind=KIND, page=page, page_size=100)
            for task in rows["tasks"]:
                if task["status"] in ACTIVE:
                    store.update(
                        task["task_id"],
                        status="needs_attention",
                        error="Gateway再起動によりfixtureの結果未確認",
                    )
            if not rows["pagination"]["has_more"]:
                break
            page += 1

    def latest(self, session, owner):
        tasks = self.store.list(kind=KIND, owner_session_id=session, limit=100)
        return next((t for t in tasks if t.get("owner_user_id") == owner), None)

    def get(self, body, owner):
        task = self.store.get(body.task_id or "")
        if (
            not task
            or task["kind"] != KIND
            or task["owner_session_id"] != body.session_id
            or task.get("owner_user_id") != owner
        ):
            raise ValueError("このセッションの計画が見つかりません")
        if task["artifacts"]["plan_sha256"] != body.plan_sha256:
            raise ValueError("計画が変更されました。最新の経路を確認してください")
        return task

    def select(self, body, owner):
        with self.lock:
            prior = self.latest(body.session_id, owner)
            if prior and prior["status"] in ACTIVE:
                raise ValueError("飛行中の目的地変更はできません。中止と終了確認が必要です")
            # Invalidate before validating the new marker: an invalid selection
            # must never leave an older approval dispatchable.
            if prior and prior["status"] in {"proposed", "approved"}:
                self.store.update(prior["task_id"], status="superseded")
            plan = self.planner.plan(body.goal_xy_m)
            if self.backend == "fixture" and plan["mission_judge"].get("mode") == "live":
                raise ValueError("Live Jev requires the CPU PX4/Gazebo backend")
            if self.backend == "fixture":
                plan["execution_target"] = "cpu_kinematic_fixture"
            task = self.store.create(
                kind=KIND,
                title="横浜3Dマップ配送（CPU fixture）",
                status="proposed",
                owner_session_id=body.session_id,
                owner_user_id=owner,
                artifacts=dict(plan=plan, plan_sha256=digest(plan)),
                metadata=dict(
                    execution_target=plan["execution_target"], physical_execution_invoked=False
                ),
            )
            self.store.append_event(
                task["task_id"],
                event_type="map_goal_selected",
                payload=dict(plan_sha256=digest(plan), goal=plan["goal_source_xyz_m"]),
            )
            return task

    def action(self, name, body, owner):
        with self.lock:
            task = self.get(body, owner)
            identity = task["task_id"]
            latest = self.latest(body.session_id, owner)
            if not latest or latest["task_id"] != identity:
                raise ValueError("別タブで計画が更新されました。最新状態を再読込してください")
            if name == "invalidate":
                if task["status"] in ACTIVE:
                    raise ValueError("飛行中のマーカー変更は拒否されました")
                if task["status"] in {"proposed", "approved"}:
                    return self.store.update(identity, status="superseded")
                return task
            if name == "cancel":
                if task["status"] in ACTIVE:
                    self.cancel_event.set()
                    return self.store.update(identity, status="cancel_requested")
                if task["status"] in {"proposed", "approved"}:
                    return self.store.update(identity, status="canceled")
                return task
            plan = task["artifacts"]["plan"]
            if digest(plan) != task["artifacts"]["plan_sha256"]:
                raise ValueError("保存計画の整合性が失われました")
            self.planner.current()
            from src.intelligence.yokohama_pad_jev import configuration
            if plan["mission_judge"] != configuration():
                raise ValueError("判断mode・予算が変更されました。再計画してください")
            if name == "approve":
                if task["status"] == "approved":
                    return task
                if task["status"] != "proposed":
                    raise ValueError("最新計画の承認待ちではありません")
                approval = dict(
                    plan_sha256=digest(plan),
                    scene_version=plan["scene_version"],
                    actor_session_id=body.session_id,
                    actor_user_id=owner,
                    approval_ref=uuid.uuid4().hex,
                )
                self.store.append_event(identity, event_type="map_human_approved", payload=approval)
                return self.store.update(
                    identity, status="approved", artifacts=dict(approval=approval)
                )
            if name != "execute":
                raise ValueError("未対応の操作です")
            if task["status"] in ACTIVE | {"completed"}:
                return task
            approval = task["artifacts"].get("approval", {})
            if (
                task["status"] != "approved"
                or approval.get("plan_sha256") != digest(plan)
                or approval.get("scene_version") != self.planner.version
                or approval.get("actor_session_id") != body.session_id
                or approval.get("actor_user_id") != owner
            ):
                raise ValueError("goal・route・scene versionに結び付いた人間承認が必要です")
            if self.active or self.dispatch.owner is not None:
                raise ValueError("別のfixture配送を実行中です")
            opt_in = (
                "RUN_MISSIONOS_YOKOHAMA_MAP_FIXTURE"
                if self.backend == "fixture"
                else "RUN_MISSIONOS_YOKOHAMA_DELIVERY_SIM"
            )
            if os.environ.get(opt_in) != "1":
                raise ValueError("シミュレータ実行を明示的に有効にしてください")
            from src.runtime.yokohama_goal import runtime_hashes

            if runtime_hashes() != plan["runtime_source_sha256"]:
                raise ValueError("承認後にruntimeが変更されました。再計画してください")
            expected_target = (
                "cpu_kinematic_fixture" if self.backend == "fixture" else "px4_gazebo_fixture"
            )
            if plan["execution_target"] != expected_target:
                raise ValueError("承認時の実行環境が変更されました")
            if self.backend == "sitl":
                python = Path(os.environ.get("MISSIONOS_YOKOHAMA_SITL_PYTHON", sys.executable))
                if not python.is_file():
                    raise ValueError("SITL Python環境がありません")
                self._available()
            folder = self.outputs / identity
            folder.mkdir(parents=True, exist_ok=False)
            (folder / "approved.json").write_text(
                json.dumps(dict(plan=plan, approval=approval), ensure_ascii=False, allow_nan=False)
            )
            if plan["mission_judge"].get("mode") == "live":
                if self.backend != "sitl":
                    raise ValueError("Live Jev is restricted to one CPU PX4/Gazebo delivery")
                import shutil
                if shutil.disk_usage(ROOT).free < 2 * 1024**3:
                    raise ValueError("Live試験には2GiB以上の空きが必要です。追加削除は行いません")
                from src.intelligence.yokohama_jev_live import LiveLedger
                LiveLedger().claim(identity, digest(plan))
            self.dispatch.reserve(identity)
            try:
                self.active = identity
                self.cancel_event = threading.Event()
                task = self.store.update(
                    identity, status="starting", artifacts=dict(output_directory=str(folder))
                )
                self.worker = threading.Thread(
                    target=self._run, args=(identity, plan, folder), daemon=True
                )
                self.worker.start()
            except Exception:
                self.active = None
                self.worker = None
                self.dispatch.release(identity)
                try:
                    self.store.update(identity, status="needs_attention", error="配送workerを開始できませんでした")
                except Exception:
                    # Preserve the original failure when the store itself is unavailable.
                    pass
                raise
            return task

    def _run(self, identity, plan, folder):
        try:
            with self.lock:
                if not self.cancel_event.is_set():
                    self.store.update(identity, status="running")
            with (folder / "observations.jsonl").open("w") as log:

                def emit(row):
                    log.write(json.dumps(row, allow_nan=False) + "\n")
                    log.flush()
                    self.store.update(identity, artifacts=dict(observation=row))

                result = (
                    run(plan, emit, self.cancel_event, delay=self.delay)
                    if self.backend == "fixture"
                    else self._sitl(identity, plan, folder, emit)
                )
            (folder / "result.json").write_text(json.dumps(result, allow_nan=False))
            with self.lock:
                canceled = self.cancel_event.is_set()
                status = (
                    "canceled"
                    if canceled
                    else "completed"
                    if result["passed"]
                    else "needs_attention"
                )
                self.store.update(identity, status=status, artifacts=dict(result=result))
                self.store.append_event(identity, event_type="map_fixture_verified", payload=result)
        except Exception as exc:
            self.store.update(identity, status="needs_attention", error=str(exc))
        finally:
            with self.lock:
                self.active = None
                self.dispatch.release(identity)

    def _available(self):
        # Never stop or share another simulator. Original chat uses this same prefix.
        try:
            result = subprocess.run(
                ["docker", "ps", "--filter", "name=missionos-yokohama-", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            if result.stdout.strip():
                raise ValueError("別の横浜simulatorが実行中です。既存flightは停止しません")
            subprocess.run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    "px4io/px4-sitl-gazebo:latest",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("Dockerまたは既存PX4/Gazebo imageを確認できません") from exc

    def _sitl(self, identity, plan, folder, emit):
        from src.gateway.yokohama_delivery_chat import SIM_ENV, YokohamaChatService

        python = os.environ.get("MISSIONOS_YOKOHAMA_SITL_PYTHON", sys.executable)
        output = folder / "run"
        args = [
            python,
            str(ROOT / "scripts/yokohama_sitl.py"),
            "--phase",
            "flight",
            "--approve-sitl",
            "--sea-round-trip",
            "--deliver-payload",
            "--occupied-pad",
            "--decision-backend",
            "fixture",
            "--wam-profile",
            "motion-v4",
            "--goal-plan",
            str(folder / "approved.json"),
            "--output-dir",
            str(output),
            "--timeout-seconds",
            "3000",
        ]
        env = {k: v for k, v in os.environ.items() if k in SIM_ENV}
        env["MISSIONOS_YOKOHAMA_JEV_MODE"] = plan["mission_judge"]["mode"]
        if plan["mission_judge"].get("budget_id"):
            env["MISSIONOS_YOKOHAMA_JEV_BUDGET_ID"] = plan["mission_judge"]["budget_id"]
        if os.environ.get("MISSIONOS_YOKOHAMA_ALTITUDE_DIAGNOSTICS") == "1":
            args.append("--altitude-diagnostics")
        with (folder / "simulator.log").open("w") as log:
            proc = subprocess.Popen(
                args,
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self.process = proc
            deadline = time.monotonic() + 3300
            try:
                previous = None
                judged = set()
                while proc.poll() is None:
                    if self.cancel_event.wait(0.25) or time.monotonic() > deadline:
                        break
                    YokohamaChatService._judge_pending(
                        self, identity, output, {"agents": plan["mission_judge"]}, judged
                    )
                    trajectory = output / "flight-trajectory.jsonl"
                    if trajectory.is_file():
                        # Read only a bounded tail, ignoring a partially written row.
                        with trajectory.open("rb") as handle:
                            handle.seek(max(0, trajectory.stat().st_size - 8192))
                            lines = handle.read().splitlines()
                        for line in reversed(lines):
                            try:
                                row = json.loads(line)
                                xyz = row["vehicle"]["xyz"]
                                from src.runtime.yokohama_scene import to_source

                                point = to_source(xyz, plan["frame"]).tolist()
                                observation = dict(
                                    kind="pose",
                                    phase=row["phase"],
                                    xyz_m=point,
                                    plan_sha256=digest(plan),
                                )
                                if observation != previous:
                                    emit(observation)
                                    previous = observation
                                break
                            except (ValueError, KeyError):
                                continue
            finally:
                # Existing owned-process cleanup allows the runner to delete only
                # its own container before reaping its process group.
                YokohamaChatService._reap(self, proc, grace=180)
                self.process = None
        if self.cancel_event.is_set():
            return dict(passed=False, canceled=True, physical_execution_invoked=False)
        checks = {}
        for name in ("decisions", "pad_queue", "payload", "sitl"):
            path = folder / (name + "-verification.json")
            completed = subprocess.run(
                [
                    python,
                    str(ROOT / f"scripts/verify_yokohama_{name}.py"),
                    str(output),
                    "--output",
                    str(path),
                ],
                cwd=ROOT,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=log if not log.closed else subprocess.DEVNULL,
                timeout=180,
            )
            report = json.loads(path.read_text()) if path.is_file() else {}
            checks[name] = completed.returncode == 0 and report.get("status") == "passed"
        return dict(
            passed=all(checks.values()),
            verifiers=checks,
            goal_source_xyz_m=plan["goal_source_xyz_m"],
            plan_sha256=digest(plan),
            execution_target="px4_gazebo_fixture",
            physical_execution_invoked=False,
        )

    def _interrupt(self, proc):
        from src.gateway.yokohama_delivery_chat import YokohamaChatService

        return YokohamaChatService._interrupt(self, proc)

    @staticmethod
    def _kill_group(proc):
        from src.gateway.yokohama_delivery_chat import YokohamaChatService

        return YokohamaChatService._kill_group(proc)

    def close(self):
        with self.lock:
            self.cancel_event.set()
        if self.worker:
            self.worker.join(timeout=200)


def build_yokohama_map_router(store, resolve_owner):
    router = APIRouter()
    # Lazy geometry loading: the existing chat Gateway need not install the
    # optional scene planning dependencies until this surface is used.
    current = None
    init_lock = threading.Lock()

    def service():
        nonlocal current
        with init_lock:
            if current is None:
                current = MapService(store)
        return current

    @router.get("/missionos/yokohama/map")
    def ui():
        return FileResponse(
            ROOT / "src/gateway/static/yokohama_map.html", headers={"Cache-Control": "no-store"}
        )

    @router.get("/missionos/yokohama/map/assets/{filename}")
    def asset(filename: str):
        if filename == "three.min.js":
            path = service().planner.bundle / "vendor/three.min.js"
        elif filename == "scene.json":
            path = service().planner.bundle / filename
        else:
            raise HTTPException(404)
        return FileResponse(path)

    @router.get("/missionos/yokohama/map/state")
    def state(request: Request, session_id: str):
        if len(session_id) < 16 or len(session_id) > 100:
            raise HTTPException(422, "Invalid session")
        current = service()
        owner = resolve_owner(request)
        return dict(
            task=current.latest(session_id, owner),
            scene_version=current.planner.version,
            default_goal=current.planner.legacy["delivery_pad"]["center_xyz_m"][:2],
        )

    @router.post("/missionos/yokohama/map/{action}")
    def mutate(action: str, body: MapRequest, request: Request):
        try:
            current = service()
            owner = resolve_owner(request)
            return (
                current.select(body, owner)
                if action == "plan"
                else current.action(action, body, owner)
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    def shutdown():
        if current:
            current.close()

    router.add_event_handler("shutdown", shutdown)
    router.map_service = service
    return router
