"""Local, single-use vehicle-service contract for the fixed Yokohama mission.

The Gateway's protected local job directory is the trust boundary. Digests bind
approval and evidence; they are not signatures or remote authentication.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

TARGET = "px4_gazebo_yokohama_harbour_delivery"
ROUTE_ID = "yokohama_ship_to_harbour_pad_v1"
FEEDBACK_SCHEMA = "yokohama_endpoint_feedback_proposal.v1"
FEEDBACK_TARGET = "px4_gazebo_yokohama_native_endpoint_feedback"
FEEDBACK_ROUTE_ID = "yokohama_inland_endpoint_feedback_v1"
SOURCES = (
    "scripts/run_yokohama_vehicle_service.py",
    "src/runtime/yokohama_execution_service.py",
    "scripts/yokohama_sitl.py",
    "scripts/yokohama_local_preflight.py",
    "scripts/yokohama_sitl_worker.py",
    "scripts/yokohama_decision_worker.py",
    "scripts/yokohama_pad_worker.py",
    "scripts/yokohama_flight_worker.py",
    "scripts/yokohama_altitude_contract.py",
    "scripts/smoke_px4_gazebo_sitl_mission_upload.py",
    "scripts/yokohama_decision_host.py",
    "scripts/yokohama_pad_advisory_host.py",
    "src/runtime/yokohama_pad_queue.py",
    "src/runtime/yokohama_pad_advisory_contract.py",
    "src/runtime/yokohama_native.py",
    "docs/examples/yokohama-urban-scene/files.sha256.json",
    "docs/examples/yokohama-pad-state/model/model.json",
)
FEEDBACK_SOURCES = SOURCES + (
    "scripts/yokohama_goal_distance_adapter.py",
    "scripts/yokohama_endpoint_feedback.py",
    "src/runtime/yokohama_scene.py",
    "src/runtime/yokohama_shadow_http.py",
    "src/runtime/ship_aerovla_host.py",
    "scripts/verify_yokohama_decisions.py",
    "scripts/verify_yokohama_sitl.py",
    "scripts/verify_yokohama_endpoint_feedback.py",
)


def recovery_arguments(image: str) -> list[str]:
    from scripts.yokohama_local_preflight import image_id
    return ["--phase", "flight", "--endpoint-feedback", "--candidate-recovery",
            "--fixture-reject-second-candidate", "--decision-backend", "fixture",
            "--wam-profile", "motion-v4", "--timeout-seconds", "900",
            "--local-image-id", image_id(image)]


def recovery_input_hashes(root: Path) -> dict:
    # Bind the full Python execution closure, shell entrypoint, and frozen scene.
    names = sorted({str(p.relative_to(root)) for directory in ("src", "scripts", "packages")
                    for p in (root / directory).rglob("*.py")})
    names += ["scripts/ship_onboard_entrypoint.sh", *[
        "docs/examples/yokohama-urban-scene/" + name
        for name in ("scene.json", "route.json", "collision-prisms.obj",
                     "collision-footprints.geojson", "files.sha256.json")]]
    return {name: sha256((root / name).read_bytes()).hexdigest() for name in names}


def admit_recovery_request(raw: bytes, root: Path, simulator_arguments: list[str]) -> dict:
    from scripts.yokohama_candidate_recovery import LIMITS
    manifest = json.loads(raw)
    proposal, approval = manifest["proposal"], manifest["approval"]
    if (proposal.get("schema") != "yokohama.candidate-recovery-proposal.v1"
            or proposal.get("limits") != LIMITS
            or proposal.get("city_models") != "fixture"
            or proposal.get("physical_execution_invoked") is not False
            or not proposal.get("proposal_id")
            or proposal.get("simulator_arguments") != recovery_arguments(proposal.get("image_id"))
            or simulator_arguments != proposal["simulator_arguments"]
            or proposal.get("input_sha256") != recovery_input_hashes(root)
            or approval.get("approved_proposal_sha256") != proposal_digest(proposal)
            or approval.get("maximum_actual_flight_trials") != 1
            or not all(approval.get(k) for k in (
                "operator_approval_ref", "actor_session_id", "approved_at"))):
        raise ValueError("Recovery proposal/source/limits/approval mismatch")
    return manifest


def proposal_digest(value: dict) -> str:
    # Existing Gateway approval serialization: do not change separators.
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def input_hashes(root: Path, service: Path | None = None) -> dict:
    hashes = {name: sha256((root / name).read_bytes()).hexdigest() for name in SOURCES}
    if service is not None:
        hashes["native_service_config"] = sha256(service.read_bytes()).hexdigest()
    return hashes


def arguments(models: str, service: Path | None = None, judge: str = "gateway",
              *, local_image_id: str | None = None) -> list[str]:
    if models not in ("fixture", "native") or judge not in ("fixture", "gateway"):
        raise ValueError("Unsupported vehicle backend or pad judge")
    if (service is not None) != (models == "native"):
        raise ValueError("Native models require exactly one service configuration")
    args = [
        "--phase", "flight", "--sea-round-trip", "--deliver-payload", "--occupied-pad",
        "--pad-state-advisory", "assist", "--pad-mission-judge", judge,
        "--decision-backend", models, "--wam-profile", "motion-v4",
        "--timeout-seconds", "3000",
    ]
    if service is not None:
        args += ["--native-service-config", str(service)]
    if local_image_id is not None:
        from scripts.yokohama_local_preflight import image_id
        args += ["--local-image-id", image_id(local_image_id)]
    return args


def feedback_arguments(models: str) -> list[str]:
    """Separate CPU-plan catalog; it cannot dispatch native services or a flight."""
    if models not in ("fixture", "native"):
        raise ValueError("Unsupported endpoint-feedback backend")
    return [
        "--phase", "flight", "--endpoint-feedback", "--plan-only",
        "--decision-backend", models, "--wam-profile", "motion-v4",
        "--timeout-seconds", "900",
    ]


def feedback_input_hashes(root: Path) -> dict:
    return {name: sha256((root / name).read_bytes()).hexdigest() for name in FEEDBACK_SOURCES}


def validate_feedback_request(raw: bytes, root: Path) -> dict:
    return admit_feedback_request(raw, root)[0]


def admit_feedback_request(raw: bytes, root: Path) -> tuple[dict, None]:
    """Validate a newly approved CPU plan through a separate, non-dispatch API.

    The existing service executable intentionally continues using admit_request
    and rejects this schema. No native configuration or credential is opened.
    A future execution mode requires its own controller and qualification.
    """
    from scripts.yokohama_endpoint_feedback import FIXED

    manifest = json.loads(raw)
    proposal, approval = manifest["proposal"], manifest["approval"]
    if (
        proposal.get("schema_version") != FEEDBACK_SCHEMA
        or not proposal.get("proposal_id")
        or proposal.get("execution_target") != FEEDBACK_TARGET
        or proposal.get("route") != {"route_id": FEEDBACK_ROUTE_ID}
        or proposal.get("destination_id") != "yokohama_inland_entry"
        or proposal.get("execution_mode") != "plan_only"
        or proposal.get("physical_execution_invoked") is not False
        or proposal.get("endpoint_feedback_limits") != FIXED
        or any(key in proposal for key in ("pad_queue", "payload_delivery", "sea_extension"))
    ):
        raise ValueError("Unsupported endpoint-feedback CPU plan")
    if (
        approval.get("approved_proposal_sha256") != proposal_digest(proposal)
        or not approval.get("operator_approval_ref")
        or not approval.get("actor_session_id")
        or not approval.get("approved_at")
    ):
        raise ValueError("Missing or changed endpoint-feedback approval")
    if proposal.get("simulator_arguments") != feedback_arguments(proposal.get("city_models")):
        raise ValueError("Arguments differ from the endpoint-feedback CPU-plan catalog")
    if proposal.get("input_sha256") != feedback_input_hashes(root):
        raise ValueError("Approved endpoint-feedback inputs changed")
    return manifest, None


def validate_request(raw: bytes, root: Path) -> dict:
    """Validate before importing the simulator or starting any model/child."""
    return admit_request(raw, root)[0]


def admit_request(raw: bytes, root: Path) -> tuple[dict, bytes | None]:
    """Return the exact native configuration bytes whose digest was admitted."""
    manifest = json.loads(raw)
    proposal, approval = manifest["proposal"], manifest["approval"]
    if (
        proposal.get("schema_version") != "yokohama_chat_proposal.v1"
        or not proposal.get("proposal_id")
        or proposal.get("execution_target") != TARGET
        or proposal.get("route", {}).get("route_id") != ROUTE_ID
        or proposal.get("destination_id") != "yokohama_harbour_pad"
        or proposal.get("physical_execution_invoked") is not False
    ):
        raise ValueError("Unsupported fixed-route mission")
    if (
        approval.get("approved_proposal_sha256") != proposal_digest(proposal)
        or not approval.get("operator_approval_ref")
        or not approval.get("actor_session_id")
        or not approval.get("approved_at")
    ):
        raise ValueError("Missing or changed operator approval")
    limits = proposal.get("pad_queue", {}).get("mission_judge", {})
    if (limits.get("may_only_add_wait") is not True
            or limits.get("max_decisions") != 2 or limits.get("max_added_wait_s") != 30):
        raise ValueError("Unsupported mission judge authority")
    supplied = proposal["simulator_arguments"]
    if not isinstance(supplied, list):
        raise ValueError("Invalid simulator arguments")
    # Only two explicit variants of a fixed argument catalog are supported.
    # No arbitrary flags, goal-plan, wind, recovery, or executable are accepted.
    service = None
    if "--native-service-config" in supplied:
        service = Path(supplied[supplied.index("--native-service-config") + 1])
        if not service.is_absolute():
            raise ValueError("Service configuration must have an absolute path")
    models = proposal["city_models"]
    image = None
    if "--local-image-id" in supplied:
        image = supplied[supplied.index("--local-image-id") + 1]
    variants = [arguments(models, service, judge, local_image_id=image) for judge in ("gateway", "fixture")]
    if supplied not in variants:
        raise ValueError("Arguments differ from the approved fixed-route catalog")
    native_bytes = service.read_bytes() if service is not None else None
    hashes = input_hashes(root)
    if native_bytes is not None:
        hashes["native_service_config"] = sha256(native_bytes).hexdigest()
    if proposal.get("input_sha256") != hashes:
        raise ValueError("Approved execution inputs changed")
    return manifest, native_bytes


def verify_receipt(folder: Path, expected_proposal: dict, expected_approval: dict) -> dict:
    """Check service/run binding, without replacing the flight verifiers."""
    job = folder.parent / "vehicle-service"
    try:
        raw = (job / "approved.json").read_bytes()
        manifest = json.loads(raw)
        receipt = json.loads((job / "status.json").read_text())
        result_raw = (folder / "result.json").read_bytes()
        result = json.loads(result_raw)
        config = json.loads((folder / "config.json").read_text())
        expected = proposal_digest(expected_proposal)
        valid = (
            receipt.get("schema") == "missionos.yokohama-vehicle-service.v1"
            and receipt.get("state") == "finished"
            and receipt.get("exit_code") == 0
            and receipt.get("proposal_sha256") == expected
            and proposal_digest(manifest["proposal"]) == expected
            and manifest["approval"] == expected_approval
            and receipt.get("proposal_id") == expected_proposal["proposal_id"]
            and receipt.get("physical_execution_invoked") is False
            and manifest["approval"]["approved_proposal_sha256"] == expected
            and receipt.get("approval_manifest_sha256") == sha256(raw).hexdigest()
            and config.get("operator_approval_manifest_sha256") == sha256(raw).hexdigest()
            and receipt.get("result_sha256") == sha256(result_raw).hexdigest()
            and receipt.get("city_models") == expected_proposal["city_models"]
            and result.get("decision_backend") == expected_proposal["city_models"]
            and result.get("status") == "passed"
            and result.get("cleanup") is True
        )
        if expected_proposal["city_models"] == "native":
            valid = valid and result.get("vla_invoked") is True and result.get("wam_invoked") is True
            native_hash = sha256((job / "native-service.json").read_bytes()).hexdigest()
            valid = (valid and native_hash == expected_proposal["input_sha256"]["native_service_config"]
                     and receipt.get("native_service_config_sha256") == native_hash
                     and result.get("native_service_config_sha256") == native_hash)
        arguments = expected_proposal["simulator_arguments"]
        if "--local-image-id" in arguments:
            valid = valid and result.get("image_id") == arguments[arguments.index("--local-image-id") + 1]
        return {"status": "passed" if valid else "failed"}
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "failed"}
