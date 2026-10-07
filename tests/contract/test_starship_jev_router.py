"""Network-mocked tests for public-only Jev routing and its authority boundary."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from threading import Event
import time
from types import SimpleNamespace

import pytest

from src.intelligence import starship_jev_router as router

REAL_PROVIDER_OPENER = router._provider_opener


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("MISSIONOS_STARSHIP_JEV_SHADOW_MODE", raising=False)

    def forbidden():
        pytest.fail("A test must install its own provider mock")

    monkeypatch.setattr(router, "_provider_opener", forbidden)


@pytest.fixture
def public_input():
    # Retry failure tests readiness at tick 0; the end sensor is noisy at tick 2.
    return (
        {
            "observations": [
                {"time_ticks": 0, "healthy": False},
                {"time_ticks": 2, "healthy": True},
            ],
            "retry_results": [{"start_ticks": 0, "end_ticks": 2, "success": False}],
        },
        {
            "time_ticks": 2,
            "attempts": 1,
            "released": 0,
            "deadline_ticks": 12,
            "max_attempts": 5,
            "payload_count": 3,
            "retry_ticks": 2,
            "wait_ticks": 1,
            "tick_s": 2,
        },
    )


def valid_body(route="bounded"):
    return json.dumps(
        {
            "model": "jev-1.13.0",
            "answers": {
                "assessment_route": {
                    "type": "choice",
                    "choice": route,
                    "confidence": 0.7,
                    "probabilities": {
                        key: 0.7 if key == route else 0.1 for key in router.ROUTE_TARGETS
                    },
                }
            },
        }
    ).encode()


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.read_limit = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit):
        self.read_limit = limit
        return self.body[:limit]


def mock_provider(monkeypatch, body=None, failure=None):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-not-a-secret")
    monkeypatch.setenv("MISSIONOS_STARSHIP_JEV_SHADOW_MODE", "live")
    seen = []
    response = FakeResponse(valid_body() if body is None else body)

    def open_request(request, timeout):
        seen.append((request, timeout))
        if failure:
            raise failure
        return response

    monkeypatch.setattr(router, "_provider_opener", lambda: SimpleNamespace(open=open_request))
    return seen, response


def assert_no_authority(result):
    assert set(result) == {"route", "target_role", "invocation"}
    inv = result["invocation"]
    assert inv["would_route_only"] is True
    for key in (
        "approval_granted",
        "dispatch",
        "executor_influenced",
        "model_value_claim",
        "raw_prompt_recorded",
        "raw_response_recorded",
        "confidence_calibrated",
    ):
        assert inv[key] is False


@pytest.mark.parametrize(
    "mode,status,route",
    [
        ("off", "disabled", None),
        ("fixture", "fixture_only", "bounded"),
    ],
)
def test_off_and_fixture_never_call_a_provider(public_input, mode, status, route):
    adapter = router.StarshipJevRouter(mode)
    result = adapter.route(*public_input)
    assert result["route"] == route
    assert result["invocation"]["status"] == status
    assert result["invocation"]["call_attempted"] is False
    assert result["invocation"]["model_inference_invoked"] is False
    assert result["invocation"]["reserved_call_slot"] is None
    assert adapter.calls_reserved == 0
    assert adapter.mode == mode and adapter.max_calls == 22
    assert_no_authority(result)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "auto"},
        {"mode": None},
        {"max_calls": 23},
        {"max_calls": -1},
        {"max_calls": True},
        {"max_calls": 1.5},
    ],
)
def test_constructor_enforces_fixed_modes_and_maximum(kwargs):
    with pytest.raises(ValueError):
        router.StarshipJevRouter(**kwargs)


def test_public_prompt_is_whitelisted_and_has_no_answer_or_hidden_metadata(public_input):
    before = deepcopy(public_input)
    payload = router.build_starship_jev_request(*public_input)
    assert public_input == before
    assert set(payload) == {"model", "state", "questions"}
    assert set(payload["state"]) == {
        "schema",
        "context",
        "public_history",
        "public_budget",
        "public_model_law",
    }
    assert set(payload["questions"]) == {"assessment_route"}
    law = payload["state"]["public_model_law"]
    assert law["recovery_times_s"] == [4, 8, 14, 18, None]
    assert law["prior_weights"] == [1, 1, 1, 1, 2]
    for key in (
        "train_seeds",
        "eval_seeds",
        "seed_rule",
        "tuning_candidates",
        "stratum_weights",
        "actual_recovery_time",
        "world_id",
        "selected_action",
        "expected_route",
    ):
        assert key not in router.canonical_request_bytes(payload).decode()
    assert "normal partial observability" in payload["state"]["context"]
    assert len(router.canonical_request_bytes(payload)) <= 16 * 1024


@pytest.mark.parametrize("location", ["history", "budget", "observation", "retry"])
@pytest.mark.parametrize("hidden_key", ["seed", "R", "world_id", "future_sensor", "answer"])
def test_hidden_extra_fields_rejected_before_network(public_input, location, hidden_key):
    history, budget = deepcopy(public_input)
    target = {
        "history": history,
        "budget": budget,
        "observation": history["observations"][0],
        "retry": history["retry_results"][0],
    }[location]
    target[hidden_key] = "must-never-be-sent"
    result = router.StarshipJevRouter("live").route(history, budget)
    assert result["invocation"]["status"] == "invalid_input"
    assert result["invocation"]["request_sha256"] is None
    assert result["invocation"]["call_attempted"] is False
    assert "must-never-be-sent" not in json.dumps(result)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda h, b: b.update(time_ticks=True),
        lambda h, b: b.update(attempts=2),
        lambda h, b: b.update(released=1),
        lambda h, b: b.update(deadline_ticks=13),
        lambda h, b: b.update(retry_ticks=1),
        lambda h, b: b.update(time_ticks=3),
        lambda h, b: h["observations"][0].update(time_ticks=1),
        lambda h, b: h["observations"][1].update(time_ticks=0),
        lambda h, b: h["observations"][1].update(time_ticks=4),
        lambda h, b: h["observations"][0].update(healthy=1),
        lambda h, b: h["retry_results"][0].update(success=0),
        lambda h, b: h["retry_results"][0].update(start_ticks=1),
        lambda h, b: h["retry_results"][0].update(end_ticks=4),
        lambda h, b: h["retry_results"].clear(),
        lambda h, b: h["observations"].insert(1, {"time_ticks": 1, "healthy": False}),
        lambda h, b: h["retry_results"].append(deepcopy(h["retry_results"][0])),
    ],
)
def test_only_complete_monotonic_public_endpoint_history_is_accepted(public_input, mutate):
    history, budget = deepcopy(public_input)
    mutate(history, budget)
    result = router.StarshipJevRouter("fixture").route(history, budget)
    assert result["invocation"]["status"] == "invalid_input"
    assert result["route"] is None
    assert_no_authority(result)


def test_all_wait_history_and_tuple_history_are_valid(public_input):
    history, budget = deepcopy(public_input)
    history["observations"] = tuple({"time_ticks": t, "healthy": bool(t % 2)} for t in range(13))
    history["retry_results"] = ()
    budget.update(time_ticks=12, attempts=0)
    result = router.StarshipJevRouter("fixture").route(history, budget)
    assert result["route"] == "bounded"
    assert result["invocation"]["status"] == "fixture_only"


def test_live_requires_both_dedicated_opt_in_and_host_key(monkeypatch, public_input):
    adapter = router.StarshipJevRouter("live")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only")
    assert adapter.route(*public_input)["invocation"]["status"] == "live_opt_in_required"
    monkeypatch.setenv("MISSIONOS_STARSHIP_JEV_SHADOW_MODE", "live")
    monkeypatch.delenv("TYPESAFE_API_KEY")
    assert adapter.route(*public_input)["invocation"]["status"] == "credential_unavailable"
    monkeypatch.setenv("TYPESAFE_API_KEY", "test\nHeader: injected")
    assert adapter.route(*public_input)["invocation"]["status"] == "credential_unavailable"
    assert adapter.calls_reserved == 0


@pytest.mark.parametrize("route", tuple(router.ROUTE_TARGETS))
def test_valid_live_route_is_only_a_typed_shadow_with_actual_byte_hashes(
    monkeypatch, public_input, route
):
    body = valid_body(route)
    seen, response = mock_provider(monkeypatch, body)
    result = router.StarshipJevRouter("live").route(*public_input)
    inv = result["invocation"]
    assert result["route"] == route and result["target_role"] == router.ROUTE_TARGETS[route]
    assert inv["status"] == "succeeded" and inv["model_id"] == "jev-1.13.0"
    assert inv["model_inference_invoked"] is True
    assert inv["response_received"] is True and inv["complete_response_observed"] is True
    assert inv["call_attempted"] is True and inv["call_succeeded"] is True
    assert inv["reserved_call_slot"] == 1
    request, timeout = seen[0]
    assert request.full_url == "https://api.typesafe.ai/v1/systemone"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer test-only-not-a-secret"
    assert 0 < timeout <= 15
    assert request.data == router.canonical_request_bytes(
        router.build_starship_jev_request(*public_input)
    )
    assert inv["request_sha256"] == hashlib.sha256(request.data).hexdigest()
    assert inv["response_sha256"] == hashlib.sha256(body).hexdigest()
    assert response.read_limit == 65537
    assert "test-only-not-a-secret" not in json.dumps(result)
    assert_no_authority(result)


def test_duplicate_inputs_are_not_cached_and_failures_consume_budget(monkeypatch, public_input):
    seen, _ = mock_provider(monkeypatch, failure=RuntimeError("private-server-and-secret"))
    adapter = router.StarshipJevRouter("live", max_calls=2)
    results = [adapter.route(*public_input) for _ in range(3)]
    assert [r["invocation"]["status"] for r in results] == [
        "transport_failed",
        "transport_failed",
        "budget_exhausted",
    ]
    assert [r["invocation"]["reserved_call_slot"] for r in results] == [1, 2, None]
    assert len(seen) == 2 and adapter.calls_reserved == 2
    assert "private-server-and-secret" not in json.dumps(results)
    for result in results[:2]:
        inv = result["invocation"]
        assert inv["model_inference_status"] == "unconfirmed_after_attempt"
        assert inv["model_inference_invoked"] is False and inv["response_sha256"] is None


def test_concurrent_reservation_never_exceeds_instance_limit(monkeypatch, public_input):
    seen, _ = mock_provider(monkeypatch)
    adapter = router.StarshipJevRouter("live", max_calls=2)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: adapter.route(*public_input), range(8)))
    assert len(seen) == 2 and adapter.calls_reserved == 2
    assert sorted(r["invocation"]["reserved_call_slot"] for r in results if r["route"]) == [1, 2]
    assert sum(r["invocation"]["status"] == "budget_exhausted" for r in results) == 6


def test_limits_are_per_instance_not_global(monkeypatch, public_input):
    seen, _ = mock_provider(monkeypatch)
    for _ in range(2):
        assert (
            router.StarshipJevRouter("live", max_calls=1).route(*public_input)["route"] == "bounded"
        )
    assert len(seen) == 2


@pytest.mark.parametrize(
    "body",
    [
        b"private-provider-plaintext",
        b"[]",
        b"{}",
        b'{"model":"jev-a","model":"jev-b"}',
        valid_body().replace(b'"choice": "bounded"', b'"choice": "execute"'),
        valid_body().replace(b'"confidence": 0.7', b'"confidence": true'),
        valid_body().replace(b'"confidence": 0.7', b'"confidence": NaN'),
        valid_body().replace(b'"bounded": 0.7', b'"bounded": 0.1'),
        valid_body().replace(b'"model": "jev-1.13.0"', b'"model": "private-text-model"'),
        valid_body().replace(b'"type": "choice"', b'"type": "choice", "approved": true'),
    ],
)
def test_invalid_responses_are_not_adopted_or_persisted(monkeypatch, public_input, body):
    mock_provider(monkeypatch, body)
    result = router.StarshipJevRouter("live").route(*public_input)
    inv = result["invocation"]
    assert result["route"] is None and result["target_role"] is None
    assert inv["status"] == "invalid_response"
    assert inv["call_attempted"] is True and inv["model_inference_invoked"] is False
    assert inv["model_inference_status"] == "unconfirmed_after_attempt"
    assert inv["response_sha256"] == hashlib.sha256(body).hexdigest()
    assert inv["model_id"] is None
    assert "private-provider-plaintext" not in json.dumps(result)
    assert_no_authority(result)


def test_oversized_response_has_no_complete_body_hash(monkeypatch, public_input):
    _, response = mock_provider(monkeypatch, b"x" * 70000)
    result = router.StarshipJevRouter("live").route(*public_input)
    inv = result["invocation"]
    assert inv["status"] == "response_too_large"
    assert response.read_limit == 65537 and inv["response_bytes_read"] == 65537
    assert inv["response_received"] is True and inv["complete_response_observed"] is False
    assert inv["response_sha256"] is None and result["route"] is None


def test_request_cap_applies_before_any_provider_attempt(monkeypatch, public_input):
    monkeypatch.setattr(router, "MAX_REQUEST_BYTES", 1)
    result = router.StarshipJevRouter("live").route(*public_input)
    assert result["invocation"]["status"] == "request_too_large"
    assert result["invocation"]["call_attempted"] is False


def test_wall_deadline_rejects_late_response_without_retry(monkeypatch, public_input):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only")
    monkeypatch.setenv("MISSIONOS_STARSHIP_JEV_SHADOW_MODE", "live")
    monkeypatch.setattr(router, "TIMEOUT_SECONDS", 0.03)
    release = Event()
    finished = Event()
    calls = []

    class SlowResponse(FakeResponse):
        def read(self, limit):
            release.wait(2)
            finished.set()
            return valid_body()

    def open_request(request, timeout):
        calls.append(request)
        return SlowResponse(b"")

    monkeypatch.setattr(router, "_provider_opener", lambda: SimpleNamespace(open=open_request))
    adapter = router.StarshipJevRouter("live", max_calls=1)
    started = time.monotonic()
    result = adapter.route(*public_input)
    elapsed = time.monotonic() - started
    snapshot = deepcopy(result)
    release.set()
    assert finished.wait(2)
    assert elapsed < 1
    assert result == snapshot
    assert result["invocation"]["status"] == "transport_timeout"
    assert result["invocation"]["model_inference_status"] == "unconfirmed_after_attempt"
    assert adapter.route(*public_input)["invocation"]["status"] == "budget_exhausted"
    assert len(calls) == 1


def test_redirects_and_environment_proxies_are_disabled(monkeypatch):
    # Rebuild the actual opener factory rather than the autouse network guard.
    captured = []
    monkeypatch.setattr(router, "build_opener", lambda *handlers: captured.extend(handlers))
    REAL_PROVIDER_OPENER()
    assert captured[0].proxies == {}
    assert (
        captured[1].redirect_request(None, None, 302, None, None, "https://elsewhere.invalid")
        is None
    )
