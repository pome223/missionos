"""Local operator commands for reviewed policy files (no implicit approval)."""

import json
from pathlib import Path

import click


def emit(value):
    click.echo(json.dumps(value, indent=2, sort_keys=True))


@click.group("assurance-policy")
def assurance_policy():
    """Review and approve bounded Assurance policies; run a fixture demo."""


@assurance_policy.command("validate")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def validate(file):
    """Show canonical content and hash. Does not approve the policy."""
    from src.intelligence.mission_assurance_policy import load_policy

    try:
        policy = load_policy(file)
        emit({"policy": policy.model_dump(), "sha256": policy.sha256, "approved": False})
    except (ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc


@assurance_policy.command("approve")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--db", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--operator", required=True)
@click.option("--sha256", required=True, help="Hash of the exact policy reviewed by the operator.")
def approve(file, db, operator, sha256):
    """Record the local operator's explicit approval of one reviewed policy."""
    from src.intelligence.mission_assurance_policy import PolicyStore, load_policy

    try:
        policy = PolicyStore(db).approve(
            load_policy(file), operator=operator, expected_sha256=sha256
        )
        emit(
            {
                "policy_sha256": policy.sha256,
                "policy_approval_recorded": True,
                "dispatch_request_sent": False,
            }
        )
    except (ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc


@assurance_policy.command("revoke")
@click.option("--db", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--sha256", required=True)
def revoke(db, sha256):
    """Revoke future policy reservations; already sent actions are unaffected."""
    from src.intelligence.mission_assurance_policy import PolicyStore

    try:
        PolicyStore(db).revoke(sha256)
        emit({"policy_sha256": sha256, "revoked": True})
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@assurance_policy.command("fixture")
@click.option("--db", required=True, type=click.Path(dir_okay=False, path_type=Path))
@click.option("--sha256", required=True)
@click.option("--proposal-id", required=True)
@click.option("--target-x", default=2.0, type=float)
@click.option(
    "--approve-action", is_flag=True, help="Explicit individual approval for this fixture."
)
def fixture(db, sha256, proposal_id, target_x, approve_action):
    """Run the real shared graphs using synthetic judgment and executor IO."""
    from src.intelligence.mission_assurance_policy import PolicyStore
    from src.runtime.mission_assurance_policy_fixture import run_fixture

    try:
        result = run_fixture(
            PolicyStore(db),
            sha256,
            proposal_id,
            target_x=target_x,
            explicit_approval=approve_action,
        )
        emit(result)
        if result["continuation"]["continuation_runtime_status"] == "blocked":
            raise click.exceptions.Exit(2)
    except (ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc


@assurance_policy.command("bind-turtlebot3")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--proposal-json", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--approval-json", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option("--output", required=True, type=click.Path(dir_okay=False, path_type=Path))
def bind_turtlebot3(file, proposal_json, approval_json, output):
    """Bind a reviewed scope to the exact initial mission approval; does not approve."""
    import yaml

    from src.intelligence.mission_assurance_policy import AssurancePolicy, digest, load_policy
    from src.runtime.turtlebot3_assurance_policy import approval_binding, mission_contract

    try:
        policy = load_policy(file)
        proposal = json.loads(proposal_json.read_text())
        approval = json.loads(approval_json.read_text())
        if (
            proposal.get("robot_profile") != "turtlebot3"
            or proposal.get("execution_target") != "ros2_nav2_turtlebot3_sim"
            or approval.get("operator_approved") is not True
            or not approval.get("approved_at")
            or not approval.get("operator_approval_ref")
        ):
            raise ValueError("raw_turtlebot3_proposal_and_initial_approval_required")
        bound = {**proposal, "assurance_policy_mission_approval_sha256": approval_binding(approval)}
        policy = AssurancePolicy.model_validate(
            {
                **policy.model_dump(),
                "mission_id": proposal["proposal_id"],
                "mission_contract_sha256": digest(mission_contract(bound)),
                "execution_scope": "simulator",
            }
        )
        if output.resolve() in {file.resolve(), proposal_json.resolve(), approval_json.resolve()}:
            raise ValueError("use_a_separate_output_file")
        with output.open("x", encoding="utf-8") as stream:
            stream.write(yaml.safe_dump(policy.model_dump(), sort_keys=False))
        emit({"output": str(output), "sha256": policy.sha256, "approved": False})
    except (ValueError, TypeError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
