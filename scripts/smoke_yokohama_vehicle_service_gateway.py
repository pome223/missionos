"""Real HTTP Gateway -> vehicle-service process smoke, using isolated fixtures.

Runs plan/approve/run through the production loopback conversation route. The
Gateway worker and vehicle-service CLI/receipt checks are real. The planner,
Docker inventory, flight runner and five flight-verifier results are fixtures.
No model API, Docker daemon, GPU, SITL, hardware or real delivery is invoked.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import copy
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from unittest.mock import patch

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
ROUTE = "/missionos/autonomy-conversation/run"

# This module exists only in the temporary source tree. It cannot import the
# production flight runner or reach a vehicle/model service.
RUNNER_FIXTURE = '''import json
import os
from hashlib import sha256
from pathlib import Path

def main(args):
    folder = Path(args[args.index("--output-dir") + 1])
    approval = Path(args[args.index("--approval-manifest") + 1])
    folder.mkdir()
    assert not any(os.environ.get(key) for key in (
        "DEEPSEEK_API_KEY", "TYPESAFE_API_KEY", "GOOGLE_API_KEY", "GATEWAY_API_KEY"
    )), "Gateway credentials reached isolated worker"
    (folder / "config.json").write_text(json.dumps({
        "operator_approval_manifest_sha256": sha256(approval.read_bytes()).hexdigest()
    }))
    (folder / "result.json").write_text(json.dumps({
        "run_id": "isolated-gateway-fixture", "status": "passed",
        "cleanup": True, "decision_backend": "fixture",
        "physical_execution_invoked": False, "model_inference_invoked": False,
        "worker_credentials_absent": True
    }))
    return 0
'''
VERIFIER_FIXTURE = '''import json
from pathlib import Path
import sys
output = Path(sys.argv[sys.argv.index("--output") + 1])
output.write_text(json.dumps({"status": "passed", "evidence_kind": "isolated_fixture"}))
'''


def _isolated_service(root, contract, verifiers):
    for name in contract.SOURCES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Isolated smoke source binding; not flight evidence.\n")
    for name in contract.SOURCES[:2]:
        (root / name).write_bytes((ROOT / name).read_bytes())
    (root / "scripts/yokohama_sitl.py").write_text(RUNNER_FIXTURE)
    for name in verifiers:
        (root / f"scripts/verify_yokohama_{name}.py").write_text(VERIFIER_FIXTURE)


def _port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _http_scenario(gateway, service, contract, isolated):
    port = _port()
    server = uvicorn.Server(uvicorn.Config(
        gateway.app, host="127.0.0.1", port=port, lifespan="on", log_level="warning"
    ))
    server_task = asyncio.create_task(server.serve())
    calls = 0
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=15, trust_env=False
        ) as client:
            for _ in range(100):
                with suppress(httpx.HTTPError):
                    if (await client.get("/health")).status_code == 200:
                        break
                if server_task.done():
                    await server_task
                    raise RuntimeError("Gateway stopped before the smoke was ready")
                await asyncio.sleep(0.05)
            else:
                raise TimeoutError("Loopback Gateway readiness timed out")

            async def request(text, context=None):
                nonlocal calls
                calls += 1
                payload = {
                    "operator_instruction": text,
                    "session_id": "yokohama-http-fixture",
                }
                if context is not None:
                    payload["mission_designer_context"] = context
                response = await client.post(ROUTE, json=payload)
                response.raise_for_status()
                result = response.json()
                assert result["routing_source"] == "yokohama_delivery_catalog"
                assert result["conversation_route_bypassed_guardrails"] is False
                return result

            planned = await request("Deliver the parcel to the harbour pad in Yokohama")
            assert planned["routed_action"] == "mission_designer_plan"
            context = planned["mission_designer"]
            identity = context["yokohama_delivery_task_id"]
            denied = await request("/run", context)
            assert denied["routed_action"] == "clarification"
            assert service.store.get(identity)["status"] == "proposed"
            assert service.process is None and not service.outputs.exists()

            forged = {**context, "mission_designer_context_sha256": "0" * 64}
            denied = await request("/approve", forged)
            assert denied["routed_action"] == "clarification"
            assert service.store.get(identity)["status"] == "proposed"
            approved = await request("/approve", context)
            assert approved["operation_result"]["summary"]["status"] == "approved"
            started = await request("/run", context)
            assert started["operation_result"]["summary"]["status"] in (
                "starting", "running", "completed"
            )
            for _ in range(200):
                observed = await request("/status", context)
                status = observed["operation_result"]["summary"]["status"]
                if status not in ("starting", "running"):
                    break
                await asyncio.sleep(0.05)
            assert status == "completed", status
            task = service.store.get(identity)
            folder = Path(task["artifacts"]["yokohama_output_directory"])
            receipt_path = folder.parent / "vehicle-service/status.json"
            receipt_bytes = receipt_path.read_bytes()
            receipt = json.loads(receipt_bytes)
            assert receipt["pid"] != os.getpid()
            assert receipt["state"] == "finished" and receipt["exit_code"] == 0
            assert contract.verify_receipt(
                folder, task["artifacts"]["yokohama_delivery_proposal"],
                task["artifacts"]["yokohama_delivery_approval"]
            )["status"] == "passed"
            assert json.loads((folder / "result.json").read_text())["worker_credentials_absent"]
            assert set(task["artifacts"]["yokohama_verification"].values()) == {"passed"}
            repeated = await request("/run", context)
            assert repeated["operation_result"]["summary"]["status"] == "completed"
            assert receipt_path.read_bytes() == receipt_bytes
            assert len(list(service.outputs.glob("*/vehicle-service/status.json"))) == 1

            # A second approved plan cannot run after one of its bound inputs
            # changes. Only an unused fixture source is changed in the temp tree.
            changed = await request("Deliver the parcel to the harbour pad in Yokohama")
            changed_context = changed["mission_designer"]
            changed_id = changed_context["yokohama_delivery_task_id"]
            await request("/approve", changed_context)
            (isolated / contract.SOURCES[-1]).write_text("# Changed isolated fixture.\n")
            denied = await request("/run", changed_context)
            assert denied["routed_action"] == "clarification"
            assert service.store.get(changed_id)["status"] == "approved"
            assert not (service.outputs / changed_id).exists()
            assert len(list(service.outputs.glob("*/vehicle-service/status.json"))) == 1
            return {
                "schema_version": "missionos.yokohama_vehicle_service_gateway_smoke.v1",
                "status": "passed", "transport": "real_loopback_http",
                "route": ROUTE, "http_post_count": calls,
                "preapproval_execution_rejected": True, "forged_context_rejected": True,
                "changed_approved_input_rejected": True,
                "repeat_run_created_no_new_service": True,
                "production_vehicle_service_process_observed": True,
                "production_service_receipt_verified": True,
                "worker_credentials_absent": True,
                "service_receipt_sha256": sha256(receipt_bytes).hexdigest(),
                "fixture_flight_verifier_count": 5,
                "flight_model_docker_gpu_calls": 0, "physical_execution_invoked": False,
                "limits": "Planner, Docker inventory, flight and five flight verifiers are fixtures."
            }
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=15)


async def main():
    with tempfile.TemporaryDirectory(prefix="missionos-yokohama-http-smoke-") as raw:
        root = Path(raw)
        # Explicit isolated environment: sentinels only, never live keys. Disable
        # dotenv too so running the public smoke cannot pick up workstation keys.
        environment = {
            "PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1",
            "MISSIONOS_YOKOHAMA_SITL_PYTHON": sys.executable,
            "MISSIONOS_YOKOHAMA_OUTPUT_ROOT": str(root / "runs"),
            "MISSIONOS_YOKOHAMA_CITY_MODELS": "fixture",
            "RUN_MISSIONOS_YOKOHAMA_DELIVERY_SIM": "1",
            "DEEPSEEK_API_KEY": "fixture-not-a-key", "TYPESAFE_API_KEY": "fixture-not-a-key",
            "GOOGLE_API_KEY": "fixture-not-a-key", "GATEWAY_API_KEY": "",
            "TASK_STORE_DB_PATH": str(root / "tasks.db"),
            "MEMORY_DB_PATH": str(root / "memory.db"),
            "AUDIT_LOG_PATH": str(root / "audit.log"),
            "COMPUTER_TRAJECTORY_DB_PATH": str(root / "computer.db"),
            "PHYSICAL_AI_VALIDATION_DB_PATH": str(root / "validation.db"),
        }
        with patch.dict(os.environ, environment, clear=True):
            from src.config import settings
            from src.runtime import task_store
            from src.runtime import yokohama_execution_service as contract
            from src.intelligence import yokohama_delivery_agents as agents
            settings_type = settings.Settings
            with patch.object(settings, "Settings", lambda: settings_type(_env_file=None)):
                settings.reset_settings()
                task_store.reset_task_store()
                from src.gateway import server as gateway_server
                from src.gateway import yokohama_delivery_chat as chat
                isolated = root / "isolated-source"
                _isolated_service(isolated, contract, chat.VERIFIERS)
                config = {
                    "provider": "fixture", "planner": {"model_id": "fixture-no-model"},
                    "judge": {"model_id": "fixture-no-model"},
                    "planner_timeout_seconds": 1, "judge_timeout_seconds": 1,
                }
                reading = {
                    "output": {"supported": True, "destination_id": agents.DESTINATION,
                               "summary": "Isolated fixture delivery proposal.", "reason": ""},
                    "invocation": {"model_id": "fixture-no-model"},
                }
                with (
                    patch.object(agents, "configuration", lambda: copy.deepcopy(config)),
                    patch.object(agents, "plan", lambda *_: copy.deepcopy(reading)),
                    patch.object(agents, "judge", side_effect=AssertionError("No model call allowed")),
                    patch.object(chat.YokohamaChatService, "running_simulators", return_value=[]),
                ):
                    gateway = gateway_server.create_missionos_gateway()
                    service = chat.service()
                    service.root = isolated
                    try:
                        return await _http_scenario(gateway, service, contract, isolated)
                    finally:
                        service.close()
                        settings.reset_settings()
                        task_store.reset_task_store()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main()), indent=2, sort_keys=True))
