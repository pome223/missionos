from io import StringIO

from click.testing import CliRunner
from rich.console import Console

import missionos_cli.cli as cli
import missionos_cli.play_command as play


class _ScriptedPromptSession:
    def __init__(self, commands: list[str]) -> None:
        self._commands = iter(commands)

    def prompt(self, _prompt) -> str:
        try:
            return next(self._commands)
        except StopIteration as exc:
            raise EOFError from exc


def test_play_click_wrapper_delegates_complete_request(monkeypatch, tmp_path) -> None:
    captured = {}
    monkeypatch.setattr(cli, "run_play_command", lambda **kwargs: captured.update(kwargs))

    result = CliRunner().invoke(
        cli.missionos,
        [
            "play",
            "--bundled-weather",
            "--forecast-hours",
            "6",
            "--flight-duration",
            "12",
            "--wind-step",
            "3",
            "--battery-coupling",
            "--gps-denied",
            "--history-path",
            str(tmp_path / "play.history"),
            "Fuji",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "destination": ("Fuji",),
        "scenario_key": None,
        "real_weather": False,
        "forecast_hours": 6,
        "flight_duration": 12.0,
        "wind_step": 3.0,
        "battery_coupling": True,
        "gps_denied": True,
        "history_path": tmp_path / "play.history",
    }


def test_play_approval_is_only_a_comparison_baseline(monkeypatch, tmp_path) -> None:
    output = StringIO()
    monkeypatch.setattr(
        play,
        "console",
        Console(file=output, force_terminal=False, color_system=None),
    )
    monkeypatch.setattr(
        play,
        "PromptSession",
        lambda **_kwargs: _ScriptedPromptSession(["approve", "quit"]),
    )

    play.run_play_command(
        destination=(),
        scenario_key=None,
        real_weather=False,
        forecast_hours=12,
        flight_duration=20.0,
        wind_step=2.0,
        battery_coupling=False,
        gps_denied=False,
        history_path=tmp_path / "play.history",
    )

    rendered = output.getvalue()
    assert "Recorded as the new baseline (human gate)" in rendered
    assert "approval is not flight" in rendered
    assert "Starting live PX4/Gazebo SITL" not in rendered
