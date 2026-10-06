"""No provider credentials, network or flight in these contract tests."""

import concurrent.futures
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace

import pytest

from src.intelligence.yokohama_pad_jev import configuration, judge, admit_mailbox
from src.runtime.task_store import TaskStore


def request():
    r = dict(
        judge_request_id="fixture",
        observation_id="pad_1",
        situation={"rules_action": "enter_delivery_approach"},
        decisions_remaining=0,
        remaining_wait_seconds=30,
        allowed_actions=["enter", "wait"],
    )
    return r


def provider(choice="continue", probabilities=None):
    def transport(payload):
        assert set(payload["state"]) == {
            "decision_contract",
            "mission_situation",
            "response_semantics",
        }
        return {
            "model": "jev-injected",
            "answers": {
                "response": {
                    "type": "choice",
                    "choice": choice,
                    "probabilities": probabilities
                    or {"continue": float(choice == "continue"), "hold": float(choice == "hold")},
                    "confidence": 1.0,
                },
                "review": {"choice": "bounded"},
            },
        }

    return transport


def test_fixture_never_uses_http_or_credentials(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(
        "src.intelligence.jev_assurance._provider_opener", lambda: pytest.fail("external API")
    )
    answer = judge(request(), configuration())
    assert answer["decision"]["action"] == "enter"
    assert answer["decision"]["wait_seconds"] == 0
    assert answer["invocation"]["inference_invoked"] is False
    assert answer["invocation"]["rationale_source"] == "adapter_template"


@pytest.mark.parametrize("choice,action,seconds", [("continue", "enter", 0), ("hold", "wait", 5)])
def test_current_issued_decision_remains_valid_at_zero_remaining(choice, action, seconds):
    answer = judge(request(), configuration(), transport=provider(choice))
    assert answer["judge_status"] == "valid"
    assert answer["decision"]["action"] == action
    assert answer["decision"]["wait_seconds"] == seconds


@pytest.mark.parametrize(
    "fault", ["invalid_distribution", "timeout", "rules_veto", "configuration"]
)
def test_failure_does_not_create_dispatch(fault):
    r, expected = request(), configuration()
    transport = provider()
    if fault == "invalid_distribution":
        transport = provider(probabilities={"continue": 0.8, "hold": 0.8})
    if fault == "timeout":

        def transport(payload):
            raise TimeoutError()

    if fault == "rules_veto":
        r["situation"]["rules_action"] = "wait"
    if fault == "configuration":
        expected = {**expected, "mode": "live"}
    answer = judge(r, expected, transport=transport)
    assert answer["judge_status"] != "valid"
    assert "decision" not in answer
    assert answer["invocation"]["external_api_calls"] == 0


def test_closed_live_configuration(monkeypatch):
    monkeypatch.setenv("MISSIONOS_YOKOHAMA_JEV_MODE", "live")
    with pytest.raises(ValueError, match="closed"):
        configuration()


def test_irreversible_slot_parallel_restart_cross_task(tmp_path):
    store = TaskStore(str(tmp_path / "budget.db"))

    def reserve(i):
        return store.reserve_external_request(
            budget_id="test", request_id=str(i), task_id="task" + str(i), maximum=2
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        slots = list(pool.map(reserve, range(4)))
    assert sorted(s for s in slots if s is not None) == [1, 2]
    reopened = TaskStore(str(tmp_path / "budget.db"))
    assert (
        reopened.reserve_external_request(budget_id="test", request_id="replay", task_id="new")
        is None
    )
    for i, s in enumerate(slots):
        if s:
            assert (
                reopened.reserve_external_request(
                    budget_id="test", request_id=str(i), task_id="other"
                )
                is None
            )


def test_slot_consumed_before_crash_without_response(tmp_path):
    path = tmp_path / "crash.db"
    script = (
        "from src.runtime.task_store import TaskStore; import os; s=TaskStore("
        + repr(str(path))
        + "); s.reserve_external_request(budget_id='test', request_id='same', task_id='old'); os._exit(7)"
    )
    r = subprocess.run([sys.executable, "-c", script], timeout=10)
    assert r.returncode == 7
    store = TaskStore(str(path))
    assert (
        store.reserve_external_request(budget_id="test", request_id="same", task_id="new") is None
    )
    assert store.reserve_external_request(budget_id="test", request_id="second", task_id="new") == 2
    assert (
        store.reserve_external_request(budget_id="test", request_id="third", task_id="new") is None
    )


def make_mailbox(root, advisory=False):
    helpers = runpy.run_path(str(Path(__file__).with_name("test_yokohama_pad_queue.py")))
    config, observation = helpers["config"](), helpers["observation"]
    config["map_plan_sha256"] = "plan"
    config["world"]["pad_queue"]["mission_judge"] = dict(
        mode="gateway", max_decisions=2, max_added_wait_s=30, judge_timeout_s=20
    )
    from src.runtime.yokohama_pad_queue import make_request, propose, MissionJudgeGate

    rows = [observation(t) for t in range(6)]
    pad = make_request(config, 0, rows)
    folder = root / "pad-decisions/000"
    folder.mkdir(parents=True)
    (folder / "request.json").write_text(json.dumps(pad))
    (root / "config.json").write_text(json.dumps(config))
    (root / "flight-trajectory.jsonl").write_text(json.dumps(rows[-1]) + "\n")
    baseline = propose(config, pad)
    if advisory:
        from scripts.yokohama_pad_advisory_host import PadAdvisoryHost
        from test_yokohama_pad_advisory import forecast
        config["world"]["pad_state_advisory"] = dict(mode="assist", camera_entity="pad_state_camera", weights_sha256="a" * 64)
        # Recreate a request bound to the actual advisory-enabled worker config.
        pad = make_request(config, 0, rows)
        (folder / "request.json").write_text(json.dumps(pad))
        (root / "config.json").write_text(json.dumps(config))
        host = PadAdvisoryHost(root, config)
        host.forecast = lambda *args: forecast(config, pad, "clear")
        try:
            baseline = host.respond(pad, folder, propose(config, pad))
        finally:
            host.close()
    gate = MissionJudgeGate(root, config)
    gate.respond(pad, baseline)
    request_path = root / "pad-judge/000/request.json"
    return request_path, json.loads(request_path.read_text()), config, pad, gate


def test_worker_elapsed_subprocess_and_production_host_mailbox(tmp_path):
    script = (
        "import runpy,pathlib; h=runpy.run_path("
        + repr(str(Path(__file__)))
        + "); h['make_mailbox'](pathlib.Path("
        + repr(str(tmp_path))
        + "))"
    )
    subprocess.run([sys.executable, "-c", script], check=True, timeout=10)
    path = tmp_path / "pad-judge/000/request.json"
    req = json.loads(path.read_text())
    admitted = admit_mailbox(tmp_path, path, req, "plan")
    assert admitted["issued_elapsed_s"] == 5  # never Unix seconds
    store = TaskStore(str(tmp_path / "fixture.db"))
    task = store.create(kind="test", title="fixture", artifacts={"plan_sha256": "plan"})
    from src.gateway.yokohama_delivery_chat import YokohamaChatService

    judged = set()
    YokohamaChatService._judge_pending(
        SimpleNamespace(store=store), task["task_id"], tmp_path, {"agents": configuration()}, judged
    )
    result = json.loads((path.parent / "response.json").read_text())
    assert result["judge_status"] == "valid"
    assert result["invocation"]["external_api_calls"] == 0
    assert result["admission"]["clock_domain"] == "owned_worker_elapsed_monotonic"
    (path.parent / "response.json").unlink()
    YokohamaChatService._judge_pending(
        SimpleNamespace(store=store), task["task_id"], tmp_path, {"agents": configuration()}, set()
    )
    assert json.loads((path.parent / "response.json").read_text())["judge_status"] == "unavailable"


@pytest.mark.parametrize(
    "fault", ["old", "future", "touch_stale_elapsed", "wrong_plan", "wrong_digest"]
)
def test_mailbox_boundaries(tmp_path, fault):
    path, req, _, _, _ = make_mailbox(tmp_path)
    if fault == "old":
        os.utime(path, (1, 1))
    if fault == "future":
        os.utime(path, (4e9, 4e9))
    if fault == "touch_stale_elapsed":
        tail = tmp_path / "flight-trajectory.jsonl"
        row = json.loads(tail.read_text())
        row["wall_s"] = 100
        tail.write_text(json.dumps(row) + "\n")
    if fault == "wrong_digest":
        req["observation_id"] = "different"
    with pytest.raises(ValueError):
        admit_mailbox(tmp_path, path, req, "other" if fault == "wrong_plan" else "plan")


def test_real_advisory_response_is_bound_and_accepted_by_production_hook(tmp_path):
    path, req, _, _, _ = make_mailbox(tmp_path, advisory=True)
    assert req["situation"]["camera_advisory"]["status"] == "computed"
    store = TaskStore(str(tmp_path / "fixture.db"))
    task = store.create(kind="test", title="fixture", artifacts={"plan_sha256": "plan"})
    from src.gateway.yokohama_delivery_chat import YokohamaChatService
    YokohamaChatService._judge_pending(SimpleNamespace(store=store), task["task_id"], tmp_path,
                                      {"agents": {"judge": configuration()}}, set())
    answer = json.loads((path.parent / "response.json").read_text())
    assert answer["judge_status"] == "valid"
    assert answer["invocation"]["external_api_calls"] == 0
    saved = path.parent / "input-response.json"
    value = json.loads(saved.read_text())
    value["advisory"]["status"] = "unavailable"
    saved.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="Unbound pre-judge"):
        admit_mailbox(tmp_path, path, req, "plan")
