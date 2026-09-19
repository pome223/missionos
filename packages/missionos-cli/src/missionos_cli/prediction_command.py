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
