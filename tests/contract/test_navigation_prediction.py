"""Real loopback transport tests; deterministic forecasts are fixtures only."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
import time

import pytest

from missionos_core.prediction import prediction_digest
from src.intelligence.mission_assurance_agent import MissionSituation
from src.prediction.navigation import (
    BACKEND_CONTRACTS,
    CONFIG_ENV,
    CONFIG_SCHEMA,
    INPUT_SCHEMA,
    MODE_ENV,
    prepare_navigation_prediction,
    revalidate_navigation_prediction,
)

POLICY_SHA = prediction_digest({"fixture_policy": "v1"})


@pytest.fixture
def endpoint():
    requests = []
    control = {"mutate": lambda response: None, "redirect": False, "raw": None, "delay": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            if control["redirect"]:
                self.send_response(302)
                self.send_header("Location", "/redirected")
                self.end_headers()
                return
            time.sleep(control["delay"])
            response = {
                "schema_version": "missionos_core_prediction.v1",
                "request_id": body["request_id"],
                "observation_id": body["observation_id"],
                "request_sha256": prediction_digest(body),
                "binding": body["binding"],
                "status": "available",
                "reason": "",
                "verification_basis": "model_inferred",
                "approval_recorded": False,
                "dispatch_authority_created": False,
                "physical_execution_invoked": False,
                "completion_claimed": False,
                "forecasts": [
                    {
                        "option_id": option["option_id"],
                        "horizon_seconds": option["horizon_seconds"],
                        "risk_score": 0.2,
                        "future_state": {"fixture_only": True},
                    }
                    for option in body["options"]
                ],
            }
            control["mutate"](response)
            data = control["raw"] if control["raw"] is not None else json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except BrokenPipeError:
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/predict", requests, control
    server.shutdown()
    server.server_close()
    thread.join()


def setup(monkeypatch, tmp_path, endpoint, *, backend="px4", mode="required"):
    mission, environment = BACKEND_CONTRACTS[backend]
    config = {
        "schema_version": CONFIG_SCHEMA,
        "backends": {
            backend: {
                "endpoint": endpoint[0],
                "binding": {
                    "model_id": "navigation-fixture",
                    "model_sha256": "a" * 64,
                    "mission_contract": mission,
                    "policy_sha256": POLICY_SHA,
                    "environment_contract": environment,
                    "input_schema": INPUT_SCHEMA,
                },
                "horizon_seconds": 5,
                "max_age_seconds": 5,
                "timeout_seconds": 1,
                "provider_kind": "fixture",
            }
        },
    }
    path = tmp_path / "navigation.json"
    path.write_text(json.dumps(config))
    monkeypatch.setenv(CONFIG_ENV, str(path))
    monkeypatch.setenv(MODE_ENV, mode)
    observed_time = datetime.now(timezone.utc)
    now = observed_time.timestamp()
    observed = observed_time.isoformat()
    situation = MissionSituation(
        situation_id="navigation-fixture",
        observed_at=observed,
        mission_contract={"objective": "fixture only"},
        progress={"task_id": "fixture-run"},
        observations={
            "runtime_telemetry": {
                "observed_at": observed,
                "sample_index": 1,
                "position": {"x": 0, "y": 0},
                "battery": 50,
            }
        },
        constraints={"mission_context": {"compiled_candidate": {"fixture_goal": [1, 0]}}},
        uncertainty={},
        source_refs=("fixture:observation",),
        source_schema_version="fixture.v1",
        input_digest="source-situation",
        execution_scope="fixture",
    )
    return situation, config, path, now


def prepare(situation, **kwargs):
    return prepare_navigation_prediction(
        situation, backend=kwargs.pop("backend", "px4"), policy_sha256=POLICY_SHA, **kwargs
    )


@pytest.mark.parametrize("backend", ["px4", "nav2"])
def test_required_admission_binds_exact_action_without_authority(
    monkeypatch, tmp_path, endpoint, backend
):
    situation, _, _, now = setup(monkeypatch, tmp_path, endpoint, backend=backend)
    updated, record = prepare(
        situation, backend=backend, action="detour", parameters={"offset_m": 1.5}, now=now
    )
    assert record["status"] == "adopted" and not record["required_blocked"]
    assert updated.observations == situation.observations
    assert updated.uncertainty["prediction_evidence"]["verification_basis"] == "model_inferred"
    assert situation.uncertainty == {}
    assert record["invocation_evidence"]["fixture_invocation"] is True
    assert record["invocation_evidence"]["model_execution_verified"] is False
    assert record["invocation_evidence"]["response_contract_validated"] is True
    assert record["dispatch_authority_created"] is False
    assert endpoint[1][0]["options"][0]["parameters"] == {"offset_m": 1.5}
    assert [option["option_id"] for option in endpoint[1][0]["options"]] == ["detour", "hold"]
    assert set(endpoint[1][0]["state"]) == {"runtime_telemetry", "compiled_candidate"}
    assert "observed_at" not in endpoint[1][0]["state"]["runtime_telemetry"]


def test_off_does_not_read_manifest_or_network(monkeypatch, tmp_path, endpoint):
    situation, _, path, _ = setup(monkeypatch, tmp_path, endpoint, mode="off")
    path.write_text("not json")
    updated, record = prepare_navigation_prediction(situation, backend="invalid")
    assert updated is situation and record["status"] == "off"
    assert endpoint[1] == []


def test_shadow_never_changes_judge_input(monkeypatch, tmp_path, endpoint):
    situation, _, _, now = setup(monkeypatch, tmp_path, endpoint, mode="shadow")
    updated, record = prepare(situation, now=now)
    assert updated is situation and record["status"] == "shadow"
    assert record["admission"]["status"] == "adopted"
    assert not record["required_blocked"]
    assert [option["option_id"] for option in endpoint[1][0]["options"]] == ["continue", "hold"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(request_sha256="wrong"),
        lambda r: r.update(observation_id="another"),
        lambda r: r["binding"].update(model_sha256="b" * 64),
        lambda r: r.update(dispatch_authority_created=True),
        lambda r: r.update(status="unavailable"),
        lambda r: r["forecasts"][0].update(horizon_seconds=10),
        lambda r: r["forecasts"][0].update(risk_score=True),
        lambda r: r["forecasts"][0].update(risk_score=float("nan")),
        lambda r: r["forecasts"][0].update(future_state="invalid"),
        lambda r: r["forecasts"].pop(),
        lambda r: r["forecasts"][1].update(option_id=r["forecasts"][0]["option_id"]),
    ],
)
def test_untrusted_response_fail_closed(monkeypatch, tmp_path, endpoint, mutation):
    situation, _, _, now = setup(monkeypatch, tmp_path, endpoint)
    endpoint[2]["mutate"] = mutation
    updated, record = prepare(situation, now=now)
    assert record["status"] == "rejected" and record["required_blocked"]
    assert "forecasts" not in updated.uncertainty.get("prediction_evidence", {})
    assert record["invocation_evidence"]["response_contract_validated"] is False


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/predict",
        "https://user:secret@example.com/predict",
        "https://example.com/predict?secret=a",
        "file:///tmp/model",
        "http://127.0.0.1:999999/predict",
    ],
)
def test_endpoint_constraints_before_network(monkeypatch, tmp_path, endpoint, url):
    situation, config, path, now = setup(monkeypatch, tmp_path, endpoint)
    config["backends"]["px4"]["endpoint"] = url
    path.write_text(json.dumps(config))
    _, record = prepare(situation, now=now)
    assert record["required_blocked"] and endpoint[1] == []
    assert "secret" not in json.dumps(record)


@pytest.mark.parametrize("kind", ["redirect", "oversize", "invalid_json"])
def test_transport_rejects_redirects_and_unbounded_responses(monkeypatch, tmp_path, endpoint, kind):
    situation, _, _, now = setup(monkeypatch, tmp_path, endpoint)
    if kind == "redirect":
        endpoint[2]["redirect"] = True
    else:
        endpoint[2]["raw"] = b"x" * (1_048_577 if kind == "oversize" else 3)
    _, record = prepare(situation, now=now)
    assert record["required_blocked"] and len(endpoint[1]) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("mission_contract", "stacking.v1"),
        ("environment_contract", "mujoco_stacking.v1"),
        ("input_schema", "stacking.v1"),
        ("policy_sha256", "b" * 64),
        ("model_sha256", "unpinned"),
    ],
)
def test_manifest_rejects_wrong_domain_and_policy(monkeypatch, tmp_path, endpoint, field, value):
    situation, config, path, now = setup(monkeypatch, tmp_path, endpoint)
    config["backends"]["px4"]["binding"][field] = value
    path.write_text(json.dumps(config))
    _, record = prepare(situation, now=now)
    assert record["required_blocked"] and not endpoint[1]


@pytest.mark.parametrize("kind", ["old", "future", "absent", "unscoped"])
def test_runtime_observation_requires_actual_fresh_source(monkeypatch, tmp_path, endpoint, kind):
    situation, _, _, now = setup(monkeypatch, tmp_path, endpoint)
    if kind == "absent":
        situation.observations["runtime_telemetry"].pop("observed_at")
    elif kind == "unscoped":
        situation = replace(situation, execution_scope="hardware")
    else:
        situation.observations["runtime_telemetry"]["observed_at"] = datetime.fromtimestamp(
            now + (10 if kind == "future" else -10), timezone.utc
        ).isoformat()
    _, record = prepare(situation, now=now)
    assert record["required_blocked"] and not endpoint[1]


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "cursor",
        "arbitration",
        "state",
        "candidate",
        "parameters",
        "policy",
        "stale",
        "config",
        "envelope",
    ],
)
def test_dispatch_rechecks_current_state_policy_candidate_age_and_config(
    monkeypatch, tmp_path, endpoint, change
):
    situation, config, path, now = setup(monkeypatch, tmp_path, endpoint)
    _, record = prepare(situation, action="detour", parameters={"distance": 1}, now=now)
    telemetry = deepcopy(situation.observations["runtime_telemetry"])
    compiled = deepcopy(situation.constraints["mission_context"]["compiled_candidate"])
    parameters = {"distance": 1}
    policy = POLICY_SHA
    if change == "state":
        telemetry["position"]["x"] = 1
    elif change == "cursor":
        telemetry["sample_index"] = 2
        telemetry["elapsed_seconds"] = 1
    elif change == "arbitration":
        telemetry["telemetry_arbitration"] = {
            "selected_cursor": "new_receipt",
            "selected_telemetry_sha256": "b" * 64,
        }
    elif change == "candidate":
        compiled["fixture_goal"] = [2, 0]
    elif change == "parameters":
        parameters["distance"] = 2
    elif change == "policy":
        policy = "b" * 64
    elif change == "config":
        config["backends"]["px4"]["binding"]["model_sha256"] = "b" * 64
        path.write_text(json.dumps(config))
    elif change == "envelope":
        record["envelope"]["forecast"]["forecasts"][0]["risk_score"] = 0.9
    receipt = revalidate_navigation_prediction(
        record,
        backend="px4",
        telemetry=telemetry,
        compiled_candidate=compiled,
        action="detour",
        parameters=parameters,
        policy_sha256=policy,
        now=now + (10 if change == "stale" else 0),
    )
    assert receipt["status"] == (
        "valid" if change in {"none", "cursor", "arbitration"} else "blocked"
    )
    assert receipt["dispatch_authority_created"] is False


@pytest.mark.parametrize("kind", ["timeout", "expired_while_waiting"])
def test_slow_http_cannot_extend_observation_validity(monkeypatch, tmp_path, endpoint, kind):
    situation, config, path, _ = setup(monkeypatch, tmp_path, endpoint)
    endpoint[2]["delay"] = 0.15
    config["backends"]["px4"]["timeout_seconds" if kind == "timeout" else "max_age_seconds"] = 0.05
    path.write_text(json.dumps(config))
    _, record = prepare(situation)
    assert record["status"] == "rejected" and record["required_blocked"]
    assert len(endpoint[1]) == 1
    if kind == "expired_while_waiting":
        assert record["admission"]["reason"] == "stale_or_future_observation"
    else:
        assert record["invocation_evidence"]["response_contract_validated"] is False


@pytest.mark.parametrize("current_mode", ["required", "invalid"])
@pytest.mark.parametrize("record", [None, {}, {"mode": "off"}, {"mode": "shadow"}])
def test_current_required_mode_cannot_reuse_unjudged_prediction(monkeypatch, current_mode, record):
    from src.prediction.navigation import navigation_prediction_required

    monkeypatch.setenv(MODE_ENV, current_mode)
    assert navigation_prediction_required(record) is True
    check = revalidate_navigation_prediction(record, backend="px4", telemetry={})
    assert check["status"] == "blocked"
    assert check["reasons"] == [
        "invalid_navigation_wam_mode"
        if current_mode == "invalid"
        else "navigation_prediction_not_adopted"
    ]


@pytest.mark.parametrize("current_mode", ["off", "shadow", "invalid"])
@pytest.mark.parametrize("stale", [False, True])
def test_frozen_required_forecast_still_revalidated_after_mode_change(
    monkeypatch, tmp_path, endpoint, current_mode, stale
):
    from src.prediction.navigation import navigation_prediction_required

    situation, _, _, now = setup(monkeypatch, tmp_path, endpoint)
    _, record = prepare(situation, now=now)
    monkeypatch.setenv(MODE_ENV, current_mode)
    assert navigation_prediction_required(record) is True
    check = revalidate_navigation_prediction(
        record,
        backend="px4",
        telemetry=situation.observations["runtime_telemetry"],
        compiled_candidate=situation.constraints["mission_context"]["compiled_candidate"],
        policy_sha256=POLICY_SHA,
        now=now + (10 if stale else 0),
    )
    assert check["status"] == ("blocked" if stale or current_mode == "invalid" else "valid")
