"""The operator bearer is host-only explicit private configuration."""
from types import SimpleNamespace
import pytest
from scripts.start_starship_gateway import read_operator_token, build_environment


def test_private_token_reader(tmp_path):
    path = tmp_path / "token"
    path.write_text("a" * 48)
    path.chmod(0o600)
    assert read_operator_token(path) == "a" * 48
    path.chmod(0o644)
    with pytest.raises(ValueError, match="token_file_invalid"):
        read_operator_token(path)
    path.chmod(0o600)
    link = tmp_path / "symlink"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="token_file_invalid"):
        read_operator_token(link)


def test_operator_token_opt_in_not_ambient(tmp_path, monkeypatch):
    monkeypatch.setenv("MISSIONOS_STARSHIP_INFLIGHT_OPERATOR_TOKEN", "ambient-secret")
    args = SimpleNamespace(port=18870, state_dir=tmp_path/"state", enable_live_models=False,
        project=None, deepseek_secret="deepseek-api-key", jev_secret="jev-api-key", fixture_planner=True)
    assert "MISSIONOS_STARSHIP_INFLIGHT_OPERATOR_TOKEN" not in build_environment(args)
    path = tmp_path / "token"
    path.write_text("b" * 48)
    path.chmod(0o600)
    args.inflight_operator_token_file = path
    args.inflight_operator_id = "scripted-local-test-operator"
    args.inflight_operator_source = "scripted_test_driver"
    env = build_environment(args)
    assert env["MISSIONOS_STARSHIP_INFLIGHT_OPERATOR_TOKEN"] == "b" * 48
    assert env["MISSIONOS_STARSHIP_INFLIGHT_OPERATOR_SOURCE"] == "scripted_test_driver"


def test_explicit_model_is_validated_and_passed_without_ambient_override(tmp_path):
    args = SimpleNamespace(port=18870, state_dir=tmp_path/"state", enable_live_models=False,
        project=None, deepseek_secret="deepseek-api-key", jev_secret="jev-api-key", fixture_planner=True,
        deepseek_model="deepseek-flash")
    assert build_environment(args)["MISSIONOS_AGENT_MISSIONOS_STARSHIP_PLANNER_AGENT_MODEL_ID"] == "deepseek-flash"
    args.deepseek_model = "bad/model"
    with pytest.raises(ValueError, match="invalid_model"):
        build_environment(args)
