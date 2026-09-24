"""Bounded depth navigation on the normal Gateway task/approval boundary.

Three static simulator profiles reuse the verified RGB-D selector and executor.
This module does not generate arbitrary city routes or claim payload delivery.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid

from scripts.px4_aerial_flight_session import atomic_json, sign_command
from scripts.px4_urban_headroom_trial import check_initial, source_hashes
from scripts.urban_headroom_contract import PROTOCOL, case_scene, planner_geometry
from scripts.urban_navigation_contract import digest

KIND = "px4_depth_navigation"
REQUEST = "px4_depth_navigation_request"
RESULT = "px4_depth_navigation_result"
APPROVALS = "missionos_sitl_execution_operator_approvals"
SCENES = ("gap", "climb", "detour")
IMAGE = "sha256:79968fe25aa19d51c49fbd4a863ea9380f4efe6d9afabd1c579ddeffeb8b8c93"
OPT_INS = (
    "RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_EXECUTION",
    "RUN_MISSION_DESIGNER_PX4_GAZEBO_SITL_LIVE_FLIGHT",
    "RUN_PX4_URBAN_WAM_TRIAL",
)


class DepthNavigationError(ValueError):
    """An unfulfilled navigation or authority contract; do not retry dispatch."""


def _require(value, message):
    if not value:
        raise DepthNavigationError(message)


@contextmanager
def _exclusive(name):
    # Process-wide flock also protects against two Gateway worker processes.
    slug = hashlib.sha256(name.encode()).hexdigest()
    path = Path(tempfile.gettempdir()) / f"missionos-depth-{os.getuid()}-{slug}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DepthNavigationError(
                "depth navigation is already in progress"
            ) from exc
        yield
    finally:
        os.close(fd)


def build_request(scene):
    _require(scene in SCENES, "unsupported static depth navigation profile")
    spec = case_scene(f"headroom_{scene}_0")
    return {
        "schema_version": "px4_depth_navigation_request.v1",
        "scene": scene,
        "scene_sha256": spec["scene_sha256"],
        "routes": spec["routes"],
        "goal_enu_m": spec["goal_enu_m"],
        "runtime_source_sha256": {
            **source_hashes(),
            "src/runtime/px4_depth_navigation.py": hashlib.sha256(
                Path(__file__).read_bytes()
            ).hexdigest(),
        },
        "image_id": IMAGE,
        "selector": "history_depth",
        "scope": "fixed_static_simulator_profiles",
        "operator_approval_required_before_dispatch": True,
        "unknown_space_certified_free": False,
        "model_invoked": False,
        "hardware_target_allowed": False,
        "physical_execution_invoked": False,
        "delivery_completion_claimed": False,
    }


def prepare(store, scene, *, owner=None):
    request = build_request(scene)
    task = store.create(
        kind=KIND,
        title=f"PX4 depth navigation: {scene}",
        status="pending",
        owner_user_id=owner,
        artifacts={REQUEST: request},
        metadata={"backend": "px4", "execution_scope": "simulator"},
    )
    return {
        "task": task,
        REQUEST: request,
        "summary": {
            "task_id": task["task_id"],
            "task_status": "pending",
            "gazebo_execution_invoked": False,
            "approval_created": False,
            "next_command": f"missionos execute-sitl --task-id {task['task_id']}",
        },
    }


def _pending(store, task_id):
    task = store.get(task_id)
    _require(task is not None and task["kind"] == KIND, "depth task not found")
    _require(
        task["status"] == "pending", "depth task must be pending; no automatic retry"
    )
    request = task["artifacts"].get(REQUEST, {})
    _require(
        request == build_request(request.get("scene")),
        "depth request or runtime changed",
    )
    return task, request


def approve(store, task_id, *, actor, now=None):
    now = time.time() if now is None else now
    with _exclusive(str(store.db_path.resolve()) + task_id):
        task, request = _pending(store, task_id)
        _require(bool(actor.strip()), "approval actor required")
        approval = {
            "schema_version": "px4_depth_navigation_approval.v1",
            "approval_id": "depth-approval-" + uuid.uuid4().hex,
            "task_id": task_id,
            "request_sha256": digest(request),
            "approval_actor": actor,
            "approved_at_unix_s": now,
            "operator_approved": True,
            "approval_status": "issued_unconsumed",
            "consumed_in_runtime": False,
            "scope": "choose_one_declared_route_then_land_in_simulator",
        }
        task = store.update(
            task_id, artifacts={APPROVALS: {approval["approval_id"]: approval}}
        )
    return {
        "task": task,
        "execution_operator_approval": approval,
        "summary": {
            "task_id": task_id,
            "execution_approval_id": approval["approval_id"],
            "approval_status": "issued_unconsumed",
            "gazebo_execution_invoked": False,
        },
    }


def _validate_approval(task, request, approval_id, now):
    approval = task["artifacts"].get(APPROVALS, {}).get(approval_id, {})
    _require(
        approval.get("schema_version") == "px4_depth_navigation_approval.v1"
        and approval.get("approval_id") == approval_id
        and approval.get("task_id") == task["task_id"]
        and approval.get("request_sha256") == digest(request)
        and approval.get("operator_approved") is True
        and approval.get("approval_status") == "issued_unconsumed"
        and approval.get("consumed_in_runtime") is False
        and bool(approval.get("approval_actor")),
        "missing, changed or consumed depth execution approval",
    )
    stamp = approval.get("approved_at_unix_s")
    _require(
        type(stamp) in (float, int) and 0 <= now - stamp <= 600,
        "depth approval expired",
    )
    return approval


def execute(store, task_id, approval_id, *, runner=None):
    """Consume stored authority once; complete only after raw evidence verification."""
    _require(
        all(os.getenv(k) == "1" for k in OPT_INS),
        "depth SITL execution requires explicit opt-in",
    )
    with (
        _exclusive("urban-depth-simulator"),
        _exclusive(str(store.db_path.resolve()) + task_id),
    ):
        task, request = _pending(store, task_id)
        approval = _validate_approval(task, request, approval_id, time.time())
        root = Path(
            os.getenv(
                "MISSIONOS_PX4_DEPTH_ARTIFACT_ROOT", "output/px4_depth_navigation"
            )
        )
        run = root.resolve() / task_id
        _require(not run.exists(), "depth runtime directory already exists; no replay")
        consumed = {
            **approval,
            "consumed_in_runtime": True,
            "approval_status": "consumed",
            "consumed_at_unix_s": time.time(),
        }
        store.update(
            task_id,
            status="running",
            artifacts={APPROVALS: {approval_id: consumed}},
            metadata={"depth_navigation_phase": "starting"},
        )

        def progress(phase, evidence=None):
            store.update(
                task_id,
                metadata={"depth_navigation_phase": phase},
                artifacts={
                    "px4_depth_navigation_progress": {
                        "phase": phase,
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        **(evidence or {}),
                    }
                },
            )

        try:
            (runner or run_live)(run, request, approval, progress)
            # A runner's return value or ACK cannot establish a flight outcome.
            result = verify_run(run, request, approval)
        except Exception as exc:
            result = {
                "schema_version": "px4_depth_navigation_result.v1",
                "result_status": "failed",
                "request_sha256": digest(request),
                "execution_approval_id": approval_id,
                "failure_kind": type(exc).__name__,
                "destination_reached": None,
                "landing_and_disarm_observed": None,
                "flight_outcome_verified": False,
                "delivery_completion_claimed": False,
                "physical_execution_invoked": False,
            }
            store.update(
                task_id,
                status="failed",
                artifacts={RESULT: result},
                metadata={"depth_navigation_phase": "failed"},
                error=str(exc),
            )
            raise DepthNavigationError(
                "depth execution or verification failed; inspect durable task evidence"
            ) from exc
        task = store.update(
            task_id,
            status="completed",
            artifacts={RESULT: result},
            metadata={"depth_navigation_phase": "verified"},
        )
        return {
            "task": task,
            RESULT: result,
            "summary": {
                "task_id": task_id,
                "task_status": "completed",
                "live_flight_status": "completed",
                "actual_sitl_flight_evidence_observed": True,
                "selected_route": result["route_id"],
                "destination_reached": True,
                "actual_land_observed": True,
                "delivery_completion_claimed": False,
                "hardware_target_allowed": False,
                "physical_execution_invoked": False,
            },
        }


def run_live(root, request, approval, progress):
    from scripts import px4_urban_wam_trial as urban
    from scripts.select_urban_depth_route import select

    asset_value = os.getenv("MISSIONOS_PX4_DEPTH_ASSETS", "")
    _require(bool(asset_value), "server-side pinned urban assets must be configured")
    assets = Path(asset_value).resolve()
    root.parent.mkdir(parents=True, exist_ok=True)
    _require(
        shutil.disk_usage(root.parent).free >= 512 * 1024 * 1024,
        "insufficient evidence storage",
    )
    family = f"headroom_{request['scene']}_0"
    scene = case_scene(family)
    session = root / "session"
    instruction = "gateway-depth-approval:" + approval["approval_id"]
    try:
        urban.setup(
            root, assets, family, None, instruction, request["image_id"], "headroom"
        )
        atomic_json(
            root / "gateway-binding.json",
            {
                "task_id": approval["task_id"],
                "request_sha256": digest(request),
                "execution_approval_id": approval["approval_id"],
            },
        )
        urban.call(
            [
                "docker",
                "exec",
                urban.NAME,
                "python3",
                "/session/scripts/urban_headroom_contact_probe.py",
            ],
            timeout=60,
        )
        atomic_json(
            root / "contact-process.json",
            {
                "exit_code": 0,
                "receipt_sha256": hashlib.sha256(
                    (session / "contact-positive-control.json").read_bytes()
                ).hexdigest(),
            },
        )
        progress("contact_control_passed")
        urban.start(root)
        initial = urban.wait_file(
            session, "status.json", lambda s: s.get("phase") == "holding", 420
        )
        check_initial(initial)
        atomic_json(root / "initial-state.json", initial)
        progress("observing_depth")
        urban.capture_history(root)
        selection = select(root, planner_geometry(scene))
        config = json.loads((session / "config.json").read_text())
        route_id = selection["route_id"]
        _require(
            route_id in request["routes"],
            "no admissible depth candidate; landing required",
        )
        selection.update(
            session_id=config["session_id"],
            scene_sha256=config["scene_sha256"],
            protocol_sha256=digest(PROTOCOL),
            route_sha256=digest(scene["routes"][route_id]),
        )
        atomic_json(session / "depth-selection.json", selection)
        command = {
            k: config[k]
            for k in ("session_id", "scene_sha256", "approved_instruction_ref")
        }
        issued = time.time()
        command.update(
            route_id=route_id,
            route_sha256=selection["route_sha256"],
            selection_sha256=hashlib.sha256(
                (session / "depth-selection.json").read_bytes()
            ).hexdigest(),
            issued_at_unix_s=issued,
            expires_at_unix_s=issued + 4,
        )
        progress(
            "route_selected",
            {
                "route_id": route_id,
                "model_invoked": False,
                "selection_is_not_execution": True,
            },
        )
        atomic_json(
            session / "urban-go.json",
            sign_command(command, (session / ".dispatch-key").read_bytes()),
        )
        outcome = urban.wait_file(
            session,
            "flight-result.json",
            lambda s: s.get("urban_verification_complete") is True,
            600,
        )
        _require(outcome.get("complete") is True, "depth route did not complete")
        progress("landing_observed")
    finally:
        if (root / "container.json").exists():
            if (session / "status.json").exists() and not (
                session / "flight-result.json"
            ).exists():
                try:
                    urban.call(
                        [
                            "docker",
                            "exec",
                            urban.NAME,
                            "pkill",
                            "-TERM",
                            "-f",
                            "^python3 /session/scripts/px4_urban_wam_trial.py",
                        ]
                    )
                    urban.wait_file(session, "flight-result.json", lambda _: True, 120)
                finally:
                    urban.cleanup(root)
            else:
                urban.cleanup(root)


def verify_run(root, request, approval):
    from scripts.verify_urban_wam_trial import verify

    binding = json.loads((root / "gateway-binding.json").read_text())
    _require(
        binding
        == {
            "task_id": approval["task_id"],
            "request_sha256": digest(request),
            "execution_approval_id": approval["approval_id"],
        },
        "Gateway flight binding differs",
    )
    config = json.loads((root / "session/config.json").read_text())
    _require(
        config["approved_instruction_ref"]
        == "gateway-depth-approval:" + approval["approval_id"],
        "flight did not use this task approval",
    )
    container = json.loads((root / "container.json").read_text())
    _require(container["image_id"] == request["image_id"], "unapproved simulator image")
    control = json.loads((root / "session/contact-positive-control.json").read_text())
    probe = json.loads((root / "contact-process.json").read_text())
    _require(
        probe["exit_code"] == 0
        and probe["receipt_sha256"]
        == hashlib.sha256(
            (root / "session/contact-positive-control.json").read_bytes()
        ).hexdigest(),
        "contact subprocess did not exit zero",
    )
    _require(
        control["subscriptions_released"] is True
        and control["probe_removed_observed"] is True
        and control["probe_contacts"] > 0,
        "contact control missing",
    )
    observed = verify(root)
    _require(
        observed["family"] == f"headroom_{request['scene']}_0"
        and observed["route_id"] in request["routes"],
        "unapproved flight profile",
    )
    return {
        **observed,
        "source_verification_schema_version": observed["schema_version"],
        "schema_version": "px4_depth_navigation_result.v1",
        "result_status": "verified",
        "request_sha256": digest(request),
        "execution_approval_id": approval["approval_id"],
        "delivery_completion_claimed": False,
        "physical_execution_invoked": False,
    }
