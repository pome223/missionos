"""Opt-in local prediction boundary; optional ML runtime is loaded lazily."""

from pathlib import Path
import click


@click.group("prediction")
def prediction_command():
    """Run mission-specific prediction integrations (no hardware dispatch)."""


@prediction_command.command("serve-stacking")
@click.option(
    "--trusted-model",
    "model",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--model-sha256", required=True)
@click.option("--policy-sha256", required=True)
@click.option("--output", required=True, type=click.Path(path_type=Path))
@click.option("--port", default=0, type=click.IntRange(0, 65535))
@click.option("--allow-simulator-decisions", is_flag=True)
def serve_stacking(**kwargs):
    """Serve a digest-pinned trusted-local checkpoint in a simulator-only session.

    Requires the monorepo runtime and optional NumPy/scikit-learn dependencies.
    Pickle files must be trusted; the hash is identity, not a sandbox.
    """
    try:
        from src.prediction.service import serve

        serve(**kwargs)
    except ImportError as exc:
        raise click.ClickException("Prediction requires the optional local ML runtime") from exc
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc


@prediction_command.command("admit-evidence")
@click.option(
    "--situation", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--evidence", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option("--max-age-seconds", required=True, type=click.FloatRange(min=0, min_open=True))
@click.option("--output", required=True, type=click.Path(path_type=Path))
def admit_evidence(situation, evidence, max_age_seconds, output):
    """Record Assurance evidence admission only; no LLM, approval, or execution."""
    import json
    import time

    try:
        from src.intelligence.mission_assurance_agent import MissionSituation
        from src.intelligence.prediction_evidence import receive_prediction_evidence

        updated, receipt = receive_prediction_evidence(
            MissionSituation.from_dict(json.loads(situation.read_text())),
            json.loads(evidence.read_text()),
            now=time.time(),
            max_age_seconds=max_age_seconds,
        )
        with output.open("x") as stream:
            json.dump(
                {"situation": updated.to_dict(), "receipt": receipt},
                stream,
                indent=2,
                allow_nan=False,
            )
        click.echo(receipt["status"] + ": " + receipt["reason"])
    except (ValueError, OSError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc


@prediction_command.command("serve-stacking-mission")
@click.option(
    "--trusted-model",
    "model",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--model-sha256", required=True)
@click.option("--policy-sha256", required=True)
@click.option("--output", required=True, type=click.Path(path_type=Path))
@click.option("--llm-model", required=True)
@click.option(
    "--llm-backend",
    type=click.Choice(["deepseek", "ollama", "jev"]),
    default="deepseek",
    show_default=True,
)
@click.option("--seed", "seeds", type=int, multiple=True, required=True)
@click.option("--approve-simulator-mission", is_flag=True)
@click.option("--operator", default="")
@click.option("--port", default=0, type=click.IntRange(0, 65535))
def serve_stacking_mission(**kwargs):
    """Run actual Assurance with a bounded simulator policy; no hardware."""
    try:
        from src.prediction.stacking_mission import serve_mission

        serve_mission(**kwargs)
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
