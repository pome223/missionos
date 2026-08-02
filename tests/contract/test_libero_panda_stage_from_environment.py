import pytest

from scripts.run_libero_panda_stage_from_environment import build_stage_command


def _environment() -> dict[str, str]:
    return {
        "MISSIONOS_PARENT_RUN_IDENTITY": "parent-run:1",
        "MISSIONOS_LIBERO_EPISODE_IDENTITY": "parent-run:1:episode:1",
        "MISSIONOS_LIBERO_RESULT_PATH": "/tmp/result.json",
        "MISSIONOS_EXPECTED_LIBERO_CONTRACT_SHA256": "a" * 64,
        "MISSIONOS_LIBERO_MODEL_PATH": "/runtime-model",
        "MISSIONOS_LIBERO_REFERENCE_MODEL_PATH": "/reference-model",
        "MISSIONOS_LIBERO_POLICY_CLIENT_HOST": "127.0.0.1",
        "MISSIONOS_LIBERO_POLICY_CLIENT_PORT": "5555",
    }


def test_stage_wrapper_binds_parent_owned_identity_and_contract() -> None:
    command = build_stage_command(_environment(), python_executable="python")

    assert command[:2] == (
        "python",
        "scripts/run_libero_panda_instrumented_live.py",
    )
    assert command[command.index("--run-identity") + 1] == "parent-run:1"
    assert command[command.index("--episode-identity") + 1] == (
        "parent-run:1:episode:1"
    )
    assert command[command.index("--expected-contract-sha256") + 1] == (
        "a" * 64
    )
    assert command[command.index("--maximum-observation-age-seconds") + 1] == (
        "30"
    )
    assert command[command.index("--output") + 1] == "/tmp/result.json"


@pytest.mark.parametrize(
    ("field", "reason"),
    (
        (
            "MISSIONOS_PARENT_RUN_IDENTITY",
            "missionos_parent_run_identity_missing",
        ),
        (
            "MISSIONOS_LIBERO_MODEL_PATH",
            "missionos_libero_model_path_missing",
        ),
    ),
)
def test_stage_wrapper_refuses_missing_server_binding(
    field: str,
    reason: str,
) -> None:
    environment = _environment()
    del environment[field]

    with pytest.raises(RuntimeError, match=reason):
        build_stage_command(environment)


def test_stage_wrapper_refuses_malformed_contract_digest() -> None:
    environment = _environment()
    environment["MISSIONOS_EXPECTED_LIBERO_CONTRACT_SHA256"] = "not-a-digest"

    with pytest.raises(
        RuntimeError,
        match="missionos_expected_libero_contract_sha256_invalid",
    ):
        build_stage_command(environment)
