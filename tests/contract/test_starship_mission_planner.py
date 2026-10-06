"""Planner authority, provider budget and secret-launcher contracts, without live APIs."""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.intelligence import starship_mission_planner as planner


@pytest.fixture(autouse=True)
def isolated_provider_budget(monkeypatch):
    monkeypatch.setattr(planner, "_deepseek_calls", 0)
    monkeypatch.setattr(planner, "_jev_calls", 0)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("MISSIONOS_STARSHIP_LIVE_MODELS_ENABLED", raising=False)


def _enable_mock_provider(monkeypatch):
    monkeypatch.setenv("MISSIONOS_STARSHIP_LIVE_MODELS_ENABLED", "1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-not-a-credential")


def _valid_response():
    return json.dumps(
        {
            "scenario": "dispenser_comparison",
            "rationale": "Synthetic proposal only.",
            "uncertainties": ["No physical validation."],
        }
    )


def _mock_response(monkeypatch, response):
    _enable_mock_provider(monkeypatch)

    async def invoke(prompt, attempt):
        planner._reserve_call(attempt)
        attempt.model_id = "deepseek/mock"
        attempt.response_received = True
        return response

    monkeypatch.setattr(planner, "_invoke_deepseek", invoke)


def test_default_off_never_loads_a_provider():
    with pytest.raises(planner.StarshipPlannerError, match="starship_planner_disabled") as error:
        planner.plan_starship_request("Starshipを再現して")
    assert error.value.invocation["call_attempted"] is False


@pytest.mark.parametrize(
    "utterance,scenario",
    [
        ("flight14_inspired", "flight14_inspired"),
        ("正常な打ち上げ", "counterfactual_nominal"),
        ("軌道投入中止", "orbit_no_go"),
        ("放出装置の故障", "dispenser_jam"),
        ("着水失敗", "landing_engine_failure"),
        ("放出方策を比較", "dispenser_comparison"),
    ],
)
def test_fixture_is_explicitly_not_model_inference(utterance, scenario):
    result = planner.plan_starship_request(utterance, "fixture")
    assert result["proposal"]["scenario"] == scenario
    assert result["invocation"]["status"] == "fixture_only"
    assert result["invocation"]["model_inference_invoked"] is False
    assert result["invocation"]["approval_granted"] is False


@pytest.mark.parametrize("utterance", [None, 123, "", "  ", "a" * 2001])
def test_input_is_only_a_bounded_operator_utterance(utterance):
    with pytest.raises(planner.StarshipPlannerError, match="invalid_utterance"):
        planner.plan_starship_request(utterance, "fixture")


def test_live_opt_in_and_credential_required(monkeypatch):
    with pytest.raises(planner.StarshipPlannerError, match="live_opt_in_required"):
        planner.plan_starship_request("Starship", "deepseek")
    monkeypatch.setenv("MISSIONOS_STARSHIP_LIVE_MODELS_ENABLED", "1")
    with pytest.raises(planner.StarshipPlannerError, match="credential_unavailable"):
        planner.plan_starship_request("Starship", "deepseek")
    assert planner._deepseek_calls == 0


def test_provider_receives_only_current_utterance_and_static_scenarios(monkeypatch):
    _enable_mock_provider(monkeypatch)
    seen = []

    async def invoke(prompt, attempt):
        seen.append(json.loads(prompt))
        planner._reserve_call(attempt)
        attempt.response_received = True
        return _valid_response()

    monkeypatch.setattr(planner, "_invoke_deepseek", invoke)
    result = planner.plan_starship_request("Current operator request", "deepseek")
    assert set(seen[0]) == {"operator_utterance", "scenarios"}
    assert seen[0]["operator_utterance"] == "Current operator request"
    assert "test-only-not-a-credential" not in json.dumps(seen)
    assert set(result) == {"proposal", "invocation"}
    assert "Current operator request" not in json.dumps(result)
    assert result["invocation"]["model_inference_invoked"] is True
    assert result["invocation"]["raw_prompt_recorded"] is False
    assert result["invocation"]["raw_response_recorded"] is False


@pytest.mark.parametrize(
    "response",
    [
        "[]",
        "null",
        "{}",
        "```json\n{}\n```",
        '{"scenario":"flight14_inspired","scenario":"dispenser_jam","rationale":"x","uncertainties":["x"]}',
        json.dumps({"scenario": "arbitrary_command", "rationale": "x", "uncertainties": ["x"]}),
        json.dumps(
            {
                "scenario": "flight14_inspired",
                "rationale": "x",
                "uncertainties": [],
                "approved": True,
            }
        ),
        json.dumps(
            {
                "scenario": "flight14_inspired",
                "rationale": "x",
                "uncertainties": [{"dispatch": True}],
            }
        ),
        json.dumps(
            {"scenario": "flight14_inspired", "rationale": "x" * 501, "uncertainties": ["x"]}
        ),
    ],
)
def test_invalid_or_authority_bearing_model_output_is_rejected(monkeypatch, response):
    _mock_response(monkeypatch, response)
    with pytest.raises(planner.StarshipPlannerError, match="invalid_response") as error:
        planner.plan_starship_request("Select a study", "deepseek")
    assert error.value.invocation["call_succeeded"] is True
    assert error.value.invocation["approval_granted"] is False
    assert error.value.invocation["response_sha256"]


def test_attempt_budget_is_two_even_for_invalid_model_outputs(monkeypatch):
    _mock_response(monkeypatch, "invalid JSON")
    for _ in range(2):
        with pytest.raises(planner.StarshipPlannerError, match="invalid_response"):
            planner.plan_starship_request("Select", "deepseek")
    with pytest.raises(planner.StarshipPlannerError, match="call_budget_exhausted") as error:
        planner.plan_starship_request("Select", "deepseek")
    assert error.value.invocation["call_attempted"] is False
    assert planner._deepseek_calls == 2


def test_parallel_reservations_cannot_exceed_two():
    def reserve(_):
        attempt = planner._Attempt()
        try:
            planner._reserve_call(attempt)
        except RuntimeError:
            pass
        return attempt.call_attempted

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(reserve, range(10))) == 2


def test_provider_failure_does_not_publish_secret_or_claim_inference(monkeypatch):
    _enable_mock_provider(monkeypatch)

    async def invoke(prompt, attempt):
        planner._reserve_call(attempt)
        raise RuntimeError("provider message includes test-only-not-a-credential")

    monkeypatch.setattr(planner, "_invoke_deepseek", invoke)
    with pytest.raises(planner.StarshipPlannerError, match="provider_failed") as error:
        planner.plan_starship_request("Select", "deepseek")
    assert error.value.invocation["call_attempted"] is True
    assert error.value.invocation["model_inference_invoked"] is False
    assert "test-only-not-a-credential" not in str(error.value)
    assert "test-only-not-a-credential" not in json.dumps(error.value.invocation)


def test_timeout_keeps_attempt_fact_without_claiming_response(monkeypatch):
    _enable_mock_provider(monkeypatch)
    monkeypatch.setattr(planner, "TIMEOUT_SECONDS", 0.005)

    async def invoke(prompt, attempt):
        planner._reserve_call(attempt)
        await asyncio.sleep(1)

    monkeypatch.setattr(planner, "_invoke_deepseek", invoke)
    with pytest.raises(planner.StarshipPlannerError, match="timeout") as error:
        planner.plan_starship_request("Select", "deepseek")
    assert error.value.invocation["call_attempted"] is True
    assert error.value.invocation["call_succeeded"] is False


def test_adk_boundary_uses_memory_session_one_call_and_bounded_client(monkeypatch):
    from google.adk.models.lite_llm import LiteLlm, LiteLLMClient
    from google.adk import runners
    from src.agents import model_config

    _enable_mock_provider(monkeypatch)
    seen = []
    model = LiteLlm(model="deepseek/test", api_base="https://api.deepseek.com")
    monkeypatch.setattr(model_config, "resolve_agent_model", lambda **kwargs: model)

    async def transport(self, *args, **kwargs):
        seen.append(kwargs)
        return object()

    class Runner:
        def __init__(self, *, agent, app_name, session_service):
            assert type(session_service).__name__ == "InMemorySessionService"
            assert agent.tools == [] and agent.sub_agents == []
            assert agent.generate_content_config.max_output_tokens == 600
            self.agent = agent

        async def run_async(self, **kwargs):
            assert kwargs["run_config"].max_llm_calls == 1
            await self.agent.model.llm_client.acompletion(
                model="deepseek/test",
                messages=[],
                tools=None,
                stream=False,
                num_retries=5,
                timeout=300,
                max_tokens=100000,
            )
            with pytest.raises(RuntimeError, match="retry_forbidden"):
                await self.agent.model.llm_client.acompletion(
                    model="deepseek/test", messages=[], tools=None
                )
            yield SimpleNamespace(
                is_final_response=lambda: True,
                content=SimpleNamespace(parts=[SimpleNamespace(text=_valid_response())]),
            )

    monkeypatch.setattr(LiteLLMClient, "acompletion", transport)
    monkeypatch.setattr(runners, "Runner", Runner)
    result = planner.plan_starship_request("Select study", "deepseek")
    assert len(seen) == 1
    assert seen[0]["num_retries"] == 0
    assert seen[0]["timeout"] == 25
    assert seen[0]["max_tokens"] == 600
    assert result["invocation"]["call_succeeded"] is True


def test_resolved_provider_url_must_be_pinned_before_attempt(monkeypatch):
    from google.adk.models.lite_llm import LiteLlm
    from src.agents import model_config

    _enable_mock_provider(monkeypatch)
    model = LiteLlm(model="deepseek/test", api_base="https://untrusted.invalid")
    monkeypatch.setattr(model_config, "resolve_agent_model", lambda **kwargs: model)
    with pytest.raises(planner.StarshipPlannerError, match="endpoint_rejected") as error:
        planner.plan_starship_request("Select", "deepseek")
    assert error.value.invocation["call_attempted"] is False
    assert planner._deepseek_calls == 0


@pytest.fixture
def launcher():
    path = Path(__file__).resolve().parents[2] / "scripts/start_starship_gateway.py"
    spec = importlib.util.spec_from_file_location("starship_launcher_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _launcher_args(tmp_path, **overrides):
    values = dict(
        state_dir=tmp_path / "isolated",
        port=18794,
        project=None,
        deepseek_secret="deepseek-api-key",
        jev_secret="jev-api-key",
        enable_live_models=False,
        fixture_planner=False,
    )
    return argparse.Namespace(**{**values, **overrides})


def test_launcher_default_off_drops_inherited_keys_and_backend_routes(
    launcher, tmp_path, monkeypatch
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "inherited-secret")
    monkeypatch.setenv("TYPESAFE_API_KEY", "inherited-secret")
    monkeypatch.setenv("REDIS_URL", "redis://private-host")
    monkeypatch.setenv("RUN_MISSIONOS_LIVE_SITL", "1")
    monkeypatch.setattr(
        launcher.subprocess, "run", lambda *a, **k: pytest.fail("Secret access while off")
    )
    env = launcher.build_environment(_launcher_args(tmp_path))
    assert env["MISSIONOS_STARSHIP_PLANNER_MODE"] == "off"
    assert env["GATEWAY_HOST"] == "127.0.0.1"
    assert env["MISSIONOS_STARSHIP_STATE_DIR"] == str(tmp_path / "isolated" / "runs")
    assert "MISSIONOS_STARSHIP_OUTPUT_ROOT" not in env
    assert not {
        "DEEPSEEK_API_KEY",
        "TYPESAFE_API_KEY",
        "REDIS_URL",
        "RUN_MISSIONOS_LIVE_SITL",
    } & set(env)


def test_launcher_captures_secret_without_argv_payload_or_parent_env(
    launcher, tmp_path, monkeypatch
):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"mock-secret-payload", stderr=b"")

    monkeypatch.setattr(launcher.subprocess, "run", run)
    env = launcher.build_environment(
        _launcher_args(tmp_path, project="test-project", enable_live_models=True)
    )
    assert len(calls) == 2
    assert all(kwargs["capture_output"] and kwargs["timeout"] == 30 for _, kwargs in calls)
    assert "mock-secret-payload" not in json.dumps(calls)
    assert env["DEEPSEEK_API_KEY"] == env["TYPESAFE_API_KEY"] == "mock-secret-payload"
    assert env["MISSIONOS_STARSHIP_PLANNER_MODE"] == "deepseek"
    assert "DEEPSEEK_API_KEY" not in launcher.os.environ


def test_launcher_sanitizes_secret_failure(launcher, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("sensitive provider error")

    monkeypatch.setattr(launcher.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="^starship_gateway_secret_unavailable$"):
        launcher.build_environment(
            _launcher_args(tmp_path, project="test-project", enable_live_models=True)
        )


def test_launcher_rejects_dotenv_before_secret_lookup(launcher, tmp_path, monkeypatch):
    args = _launcher_args(tmp_path, project="test-project", enable_live_models=True)
    args.state_dir.mkdir()
    (args.state_dir / ".env").write_text("# operator config belongs in another state directory\n")
    monkeypatch.setattr(
        launcher.subprocess, "run", lambda *a, **k: pytest.fail("premature secret lookup")
    )
    with pytest.raises(ValueError, match="isolated_state_required"):
        launcher.build_environment(args)


def _jev_response():
    return {
        "model": "jev-test-1.0",
        "answers": {
            "response": {
                "type": "choice",
                "choice": "request_observation",
                "probabilities": {"request_observation": 0.9, "propose_abort": 0.1},
                "confidence": 0.8,
            }
        },
    }


def _mock_jev_opener(monkeypatch, raw=None, *, error=None):
    monkeypatch.setenv("MISSIONOS_STARSHIP_LIVE_MODELS_ENABLED", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "mock-jev-credential")
    raw = json.dumps(_jev_response()).encode() if raw is None else raw
    sent = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, maximum):
            assert maximum == planner.JEV_RESPONSE_MAX_BYTES + 1
            return raw[:maximum]

    class Opener:
        def open(self, request, *, timeout):
            sent.append(request)
            assert timeout == 15
            if error is not None:
                raise error
            return Response()

    def build(*handlers):
        assert any(isinstance(handler, planner._NoRedirect) for handler in handlers)
        assert any(
            isinstance(handler, planner.ProxyHandler) and handler.proxies == {}
            for handler in handlers
        )
        return Opener()

    monkeypatch.setattr(planner, "build_opener", build)
    return sent


def test_jev_requires_live_opt_in_and_credential(monkeypatch):
    with pytest.raises(planner.StarshipPlannerError, match="live_opt_in_required"):
        planner.probe_starship_jev()
    monkeypatch.setenv("MISSIONOS_STARSHIP_LIVE_MODELS_ENABLED", "1")
    with pytest.raises(planner.StarshipPlannerError, match="credential_unavailable"):
        planner.probe_starship_jev()
    assert planner._jev_calls == 0


def test_jev_probe_is_fixed_synthetic_no_redirect_one_attempt_and_no_raw_output(monkeypatch):
    sent = _mock_jev_opener(monkeypatch)
    result = planner.probe_starship_jev()
    assert len(sent) == 1
    assert sent[0].full_url == "https://api.typesafe.ai/v1/systemone"
    assert sent[0].get_header("Authorization") == "Bearer mock-jev-credential"
    payload = json.loads(sent[0].data)
    assert payload["state"]["synthetic"] is True
    assert len(sent[0].data) < 4096
    assert "mock-jev-credential" not in sent[0].data.decode()
    assert "mock-jev-credential" not in json.dumps(result)
    assert result["diagnostics"]["choice"] == "request_observation"
    assert result["diagnostics"]["mission_judgment_performed"] is False
    assert result["invocation"]["model_inference_invoked"] is True
    assert result["invocation"]["response_sha256"]
    assert result["invocation"]["raw_response_recorded"] is False
    with pytest.raises(planner.StarshipPlannerError, match="call_budget_exhausted") as error:
        planner.probe_starship_jev()
    assert error.value.invocation["call_attempted"] is False
    assert len(sent) == 1


def test_jev_redirect_handler_never_creates_followup_request():
    assert (
        planner._NoRedirect().redirect_request(
            None, None, 302, "redirect", {}, "https://other.invalid"
        )
        is None
    )


def test_jev_failure_reserves_budget_and_redacts_provider_text(monkeypatch):
    sent = _mock_jev_opener(monkeypatch, error=RuntimeError("provider exposed mock-jev-credential"))
    with pytest.raises(planner.StarshipPlannerError, match="provider_failed") as error:
        planner.probe_starship_jev()
    assert len(sent) == 1
    assert error.value.invocation["call_attempted"] is True
    assert error.value.invocation["model_inference_invoked"] is False
    assert "mock-jev-credential" not in str(error.value)
    assert "mock-jev-credential" not in json.dumps(error.value.invocation)
    with pytest.raises(planner.StarshipPlannerError, match="call_budget_exhausted"):
        planner.probe_starship_jev()
    assert len(sent) == 1


@pytest.mark.parametrize(
    "mutation", ["choice", "probability", "confidence", "sum", "model", "type"]
)
def test_jev_probe_rejects_untyped_diagnostics(monkeypatch, mutation):
    raw = _jev_response()
    answer = raw["answers"]["response"]
    if mutation == "choice":
        answer["choice"] = "execute_now"
    elif mutation == "probability":
        answer["probabilities"]["request_observation"] = True
    elif mutation == "confidence":
        answer["confidence"] = float("nan")
    elif mutation == "sum":
        answer["probabilities"] = {"request_observation": 0.8, "propose_abort": 0.8}
    elif mutation == "model":
        raw["model"] = "arbitrary provider text"
    elif mutation == "type":
        answer["type"] = "free_text"
    _mock_jev_opener(monkeypatch, json.dumps(raw).encode())
    with pytest.raises(planner.StarshipPlannerError, match="invalid_response") as error:
        planner.probe_starship_jev()
    assert error.value.invocation["model_inference_invoked"] is False
    assert error.value.invocation["response_sha256"]


def test_jev_response_size_is_bounded(monkeypatch):
    _mock_jev_opener(monkeypatch, b"x" * (planner.JEV_RESPONSE_MAX_BYTES + 1))
    with pytest.raises(planner.StarshipPlannerError, match="response_oversize"):
        planner.probe_starship_jev()
    assert planner._jev_calls == 1
