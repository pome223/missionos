"""Operator-selected Jev modes reach the launched Gateway unchanged."""

import os
from types import SimpleNamespace

import pytest

from scripts import run_agent_graph_gateway as launcher


class Launched(Exception):
    pass


@pytest.mark.parametrize(
    "cli,environment,dotenv,expected",
    [
        (None, None, None, "off"),
        (None, None, "shadow", "shadow"),
        (None, None, "primary", "primary"),
        (None, "off", "shadow", "off"),
        (None, "primary", "off", "primary"),
        ("off", "shadow", "primary", "off"),
        ("shadow", "off", "primary", "shadow"),
    ],
)
def test_mode_precedence_and_optional_secret(
    monkeypatch, tmp_path, cli, environment, dotenv, expected
):
    state = tmp_path / "state"
    state.mkdir()
    if dotenv is not None:
        (state / ".env").write_text(f"MISSIONOS_JEV_MODE={dotenv}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MISSIONOS_JEV_MODE", raising=False)
    if environment is not None:
        monkeypatch.setenv("MISSIONOS_JEV_MODE", environment)
    argv = [
        "launcher",
        "--state-root",
        str(state),
        "--task-db",
        str(state / "tasks.db"),
        "--secret-project",
        "fixture-project",
    ]
    if cli is not None:
        argv.extend(["--jev-mode", cli])
    monkeypatch.setattr(launcher.sys, "argv", argv)
    secrets = []

    def run(command, **kwargs):
        if command[0] == "git":
            return SimpleNamespace(stdout="fixture-revision\n")
        secrets.append(command[-1])
        return SimpleNamespace(returncode=0, stdout=b"fixture-placeholder")

    def execve(executable, command, env):
        assert env["MISSIONOS_JEV_MODE"] == expected
        assert env["MISSIONOS_MISSION_ASSURANCE_ADK_ENABLED"] == "1"
        assert str(state) not in env["PYTHONPATH"].split(os.pathsep)
        raise Launched()

    monkeypatch.setattr(launcher.subprocess, "run", run)
    monkeypatch.setattr(launcher.os, "execve", execve)
    with pytest.raises(Launched):
        launcher.main()
    assert ("--secret=jev-api-key" in secrets) is (expected != "off")


def test_invalid_environment_mode_stops_before_secret_lookup(monkeypatch, tmp_path):
    monkeypatch.setenv("MISSIONOS_JEV_MODE", "typo")
    monkeypatch.setattr(
        launcher.sys,
        "argv",
        [
            "launcher",
            "--state-root",
            str(tmp_path),
            "--task-db",
            str(tmp_path / "tasks.db"),
            "--secret-project",
            "fixture-project",
        ],
    )

    def forbidden(*args, **kwargs):
        pytest.fail("invalid mode reached external process")

    monkeypatch.setattr(launcher.subprocess, "run", forbidden)
    with pytest.raises(SystemExit) as error:
        launcher.main()
    assert error.value.code == 2
