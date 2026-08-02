import argparse
import hashlib
import json
from types import SimpleNamespace

import pytest

from missionos_core import canonical_sha256
import scripts.run_libero_panda_instrumented_live as live_runner
from scripts.run_libero_panda_instrumented_live import (
    COSMOS_REPOSITORY,
    COSMOS_REVISION,
    EXPECTED_CONTROLLER_RUNTIME_MATERIAL,
    _failure_report,
    _positive_finite_seconds,
    _rollout_deadline,
    _verify_processor_locator,
    build_runner_configuration,
    live_controller_probe,
    observe_controller_runtime_material,
    pre_reset_ordering,
    verify_expected_contract_binding,
    verify_runtime_dependency_profile,
)
from src.runtime.libero_panda_official_runner_instrumentation import (
    LIBEROPandaInstrumentationError,
    LIBEROPandaInstrumentationEvent,
    LIBEROPandaInstrumentationRejectionReason,
)


class OperationalSpaceController(SimpleNamespace):
    pass


def test_parent_bound_contract_digest_is_checked_before_live_boundaries() -> None:
    verify_expected_contract_binding(
        prepared_contract_sha256="a" * 64,
        expected_contract_sha256="a" * 64,
    )

    with pytest.raises(LIBEROPandaInstrumentationError) as captured:
        verify_expected_contract_binding(
            prepared_contract_sha256="a" * 64,
            expected_contract_sha256="b" * 64,
        )

    assert captured.value.rejection_reason is (
        LIBEROPandaInstrumentationRejectionReason.CONTRACT_BINDING_MISMATCH
    )


def _write_json(path, value) -> str:
    content = json.dumps(value, sort_keys=True) + "\n"
    path.write_text(content, encoding="utf-8")
    return hashlib.sha256(content.encode()).hexdigest()


def _processor_kwargs() -> dict:
    return {
        "clip_outliers": True,
        "max_action_horizon": 40,
        "modality_configs": {
            "libero_sim": {
                "action": {
                    "delta_indices": list(range(16)),
                }
            }
        },
    }


def test_offline_processor_locator_override_is_content_bound(
    tmp_path,
    monkeypatch,
) -> None:
    reference_model = tmp_path / "reference"
    runtime_model = tmp_path / "runtime"
    snapshot = tmp_path / COSMOS_REVISION
    reference_model.mkdir()
    runtime_model.mkdir()
    snapshot.mkdir()
    reference_config = {
        "model_name": COSMOS_REPOSITORY,
        "model_type": "Gr00tN1d7",
        "action_horizon": 40,
    }
    runtime_config = {
        **reference_config,
        "model_name": str(snapshot),
    }
    reference_sha256 = _write_json(
        reference_model / "config.json",
        reference_config,
    )
    _write_json(runtime_model / "config.json", runtime_config)
    reference_processor = {"processor_kwargs": _processor_kwargs()}
    runtime_processor = {
        "processor_kwargs": {
            **_processor_kwargs(),
            "model_name": str(snapshot),
        }
    }
    reference_processor_sha256 = _write_json(
        reference_model / "processor_config.json",
        reference_processor,
    )
    _write_json(
        runtime_model / "processor_config.json",
        runtime_processor,
    )
    processor_hashes = {}
    for filename in (
        "config.json",
        "preprocessor_config.json",
        "tokenizer_config.json",
    ):
        content = f"{filename}\n"
        (snapshot / filename).write_text(content, encoding="utf-8")
        processor_hashes[filename] = hashlib.sha256(
            content.encode()
        ).hexdigest()
    monkeypatch.setattr(
        live_runner,
        "EXPECTED_CHECKPOINT_CONFIG_SHA256",
        reference_sha256,
    )
    monkeypatch.setattr(
        live_runner,
        "EXPECTED_CHECKPOINT_PROCESSOR_CONFIG_SHA256",
        reference_processor_sha256,
    )
    monkeypatch.setattr(
        live_runner,
        "EXPECTED_COSMOS_PROCESSOR_SHA256",
        processor_hashes,
    )

    material = _verify_processor_locator(
        model_path=runtime_model,
        reference_model_path=reference_model,
    )

    assert material["offline_local_snapshot_override_applied"] is True
    assert material["only_model_name_locator_changed"] is True
    assert material["local_path_recorded"] is False
    assert material["processor_artifact_sha256"] == processor_hashes
    assert material["model_action_horizon_capacity"] == 40
    assert material["policy_action_horizon"] == 16
    assert material["execution_horizon"] == 8


def test_offline_processor_locator_refuses_other_config_changes(
    tmp_path,
    monkeypatch,
) -> None:
    reference_model = tmp_path / "reference"
    runtime_model = tmp_path / "runtime"
    snapshot = tmp_path / COSMOS_REVISION
    reference_model.mkdir()
    runtime_model.mkdir()
    snapshot.mkdir()
    reference_config = {
        "model_name": COSMOS_REPOSITORY,
        "model_type": "Gr00tN1d7",
        "action_horizon": 40,
    }
    reference_sha256 = _write_json(
        reference_model / "config.json",
        reference_config,
    )
    reference_processor_sha256 = _write_json(
        reference_model / "processor_config.json",
        {"processor_kwargs": _processor_kwargs()},
    )
    _write_json(
        runtime_model / "processor_config.json",
        {
            "processor_kwargs": {
                **_processor_kwargs(),
                "model_name": str(snapshot),
            }
        },
    )
    _write_json(
        runtime_model / "config.json",
        {
            "model_name": str(snapshot),
            "model_type": "mutated",
        },
    )
    monkeypatch.setattr(
        live_runner,
        "EXPECTED_CHECKPOINT_CONFIG_SHA256",
        reference_sha256,
    )
    monkeypatch.setattr(
        live_runner,
        "EXPECTED_CHECKPOINT_PROCESSOR_CONFIG_SHA256",
        reference_processor_sha256,
    )

    try:
        _verify_processor_locator(
            model_path=runtime_model,
            reference_model_path=reference_model,
        )
    except RuntimeError as exc:
        assert "changed beyond the processor locator" in str(exc)
    else:
        raise AssertionError("a non-locator config mutation was accepted")


def _runtime_environment():
    controller = OperationalSpaceController(
        name="OSC_POSE",
        control_dim=6,
        use_delta=True,
        use_ori=True,
        impedance_mode="fixed",
    )
    robot = SimpleNamespace(
        controller=controller,
        gripper=SimpleNamespace(dof=1),
    )
    task = SimpleNamespace(
        robots=[robot],
        action_dim=7,
        control_freq=20,
    )
    return SimpleNamespace(_env=SimpleNamespace(env=task))


def test_live_controller_probe_binds_runtime_observation() -> None:
    env = _runtime_environment()

    material = observe_controller_runtime_material(env)
    binding = live_controller_probe(env)

    assert material == EXPECTED_CONTROLLER_RUNTIME_MATERIAL
    assert binding.action_dim == 7
    assert binding.controller_configuration_sha256 == canonical_sha256(
        EXPECTED_CONTROLLER_RUNTIME_MATERIAL
    )
    assert (
        build_runner_configuration().controller_configuration_sha256
        == binding.controller_configuration_sha256
    )


def test_live_controller_probe_refuses_profile_drift() -> None:
    env = _runtime_environment()
    env._env.env.robots[0].controller.use_delta = False

    try:
        live_controller_probe(env)
    except RuntimeError as exc:
        assert "does not match the frozen profile" in str(exc)
    else:
        raise AssertionError("controller profile drift was accepted")


def test_runtime_dependency_profile_requires_reset_verified_versions() -> None:
    verify_runtime_dependency_profile(
        {
            "robosuite": "1.4.0",
            "mujoco": "2.3.7",
        }
    )

    try:
        verify_runtime_dependency_profile(
            {
                "robosuite": "1.4.0",
                "mujoco": "3.11.0",
            }
        )
    except LIBEROPandaInstrumentationError as exc:
        assert exc.rejection_reason is (
            LIBEROPandaInstrumentationRejectionReason
            .RUNTIME_DEPENDENCY_PROFILE_MISMATCH
        )
    else:
        raise AssertionError("an unverified MuJoCo runtime was accepted")


def test_pre_reset_order_is_derived_from_observed_events() -> None:
    result = SimpleNamespace(
        event_sequence=(
            LIBEROPandaInstrumentationEvent.PREPARED_BEFORE_RESET,
            LIBEROPandaInstrumentationEvent.CONTROLLER_RUNTIME_BOUND,
            LIBEROPandaInstrumentationEvent.RESET_OBSERVED,
        )
    )
    prepared = SimpleNamespace(prepared_at="2026-07-31T00:00:00+00:00")

    ordering = pre_reset_ordering(result, prepared=prepared)

    assert ordering["order_observed"] is True
    assert ordering["prepared_event_index"] == 0
    assert ordering["controller_bound_event_index"] == 1
    assert ordering["reset_observed_event_index"] == 2


def test_pre_reset_order_refuses_reordered_events() -> None:
    result = SimpleNamespace(
        event_sequence=(
            LIBEROPandaInstrumentationEvent.RESET_OBSERVED,
            LIBEROPandaInstrumentationEvent.CONTROLLER_RUNTIME_BOUND,
            LIBEROPandaInstrumentationEvent.PREPARED_BEFORE_RESET,
        )
    )
    prepared = SimpleNamespace(prepared_at="2026-07-31T00:00:00+00:00")

    try:
        pre_reset_ordering(result, prepared=prepared)
    except RuntimeError as exc:
        assert "ordering was not observed" in str(exc)
    else:
        raise AssertionError("reordered pre-reset events were accepted")


def test_failure_report_preserves_observed_boundary_without_error_text() -> None:
    configuration = build_runner_configuration()
    prepared = SimpleNamespace(
        run_identity="run:1",
        episode_identity="run:1:episode:1",
        prepared_at="2026-07-31T00:00:00+00:00",
        contract=SimpleNamespace(to_material=lambda: {"contract": 1}),
        recorder=SimpleNamespace(
            event_sequence=(
                LIBEROPandaInstrumentationEvent.PREPARED_BEFORE_RESET,
                LIBEROPandaInstrumentationEvent.CONTROLLER_RUNTIME_BOUND,
                LIBEROPandaInstrumentationEvent.RESET_OBSERVED,
                LIBEROPandaInstrumentationEvent.POLICY_REQUEST_INVOKED,
                LIBEROPandaInstrumentationEvent.POLICY_RESPONSE_OBSERVED,
            )
        ),
    )

    try:
        raise RuntimeError("/private/path and a credential-like value")
    except RuntimeError as error:
        report = _failure_report(
            phase="official_rollout",
            error=error,
            prepared=prepared,
            revisions={"isaac_groot_revision": "pinned"},
            configuration=configuration,
            runtime={"robosuite": "1.4.0", "mujoco": "3.11.0"},
        )

    assert report["result"] == "failed"
    assert {
        key: value
        for key, value in report["failure"].items()
        if key != "exception_fingerprint"
    } == {
        "phase": "official_rollout",
        "error_type": "RuntimeError",
        "rejection_reason": "unclassified_runtime_failure",
        "error_message_included": False,
    }
    fingerprint = report["failure"]["exception_fingerprint"]
    assert fingerprint["innermost_frame_repo"] == "missionos"
    assert fingerprint["innermost_frame_path"] == (
        "tests/contract/test_libero_panda_instrumented_live_runner.py"
    )
    assert fingerprint["innermost_frame_lineno"] > 0
    assert report["prepared_episode"]["run_identity"] == "run:1"
    assert report["claim_boundary"]["missionos_contract_frozen_before_episode"]
    assert report["claim_boundary"]["missionos_instrumentation_in_loop"]
    assert report["claim_boundary"]["model_inference_invoked"]
    assert not report["claim_boundary"]["simulator_step_return_observed"]
    assert report["claim_boundary"]["physical_execution_invoked"] is False
    assert report["publication"]["local_paths_included"] is False
    assert report["runtime"] == {
        "robosuite": "1.4.0",
        "mujoco": "3.11.0",
    }


def test_self_contained_typed_rejection_has_no_exception_fingerprint() -> None:
    report = _failure_report(
        phase="official_rollout",
        error=LIBEROPandaInstrumentationError(
            "/private/path is not publication-safe",
            rejection_reason=(
                LIBEROPandaInstrumentationRejectionReason
                .POLICY_RESPONSE_ACTION_FIELDS_MISSING
            ),
        ),
    )

    assert report["schema_version"].endswith(".v3")
    assert report["failure"] == {
        "phase": "official_rollout",
        "error_type": "LIBEROPandaInstrumentationError",
        "rejection_reason": "policy_response_action_fields_missing",
        "error_message_included": False,
    }
    assert "/private/path" not in json.dumps(report)


def test_wrapped_typed_rejection_keeps_safe_cause_fingerprint() -> None:
    filename = (
        "/opt/venv/lib/python3.10/site-packages/"
        "gymnasium/envs/registration.py"
    )
    try:
        try:
            exec(
                compile(
                    "raise AttributeError('/private/value')",
                    filename,
                    "exec",
                )
            )
        except AttributeError as cause:
            raise LIBEROPandaInstrumentationError(
                "official LIBERO environment creation failed",
                rejection_reason=(
                    LIBEROPandaInstrumentationRejectionReason
                    .OFFICIAL_ENVIRONMENT_CREATION_FAILED
                ),
            ) from cause
    except LIBEROPandaInstrumentationError as error:
        report = _failure_report(
            phase="official_rollout",
            error=error,
        )

    assert {
        key: value
        for key, value in report["failure"].items()
        if key != "exception_fingerprint"
    } == {
        "phase": "official_rollout",
        "error_type": "LIBEROPandaInstrumentationError",
        "rejection_reason": "official_environment_creation_failed",
        "error_message_included": False,
    }
    assert report["failure"]["exception_fingerprint"] == {
        "innermost_frame_repo": "python_dependency",
        "innermost_frame_package": "gymnasium",
        "innermost_frame_path": "gymnasium/envs/registration.py",
        "innermost_frame_lineno": 1,
    }
    serialized = json.dumps(report)
    assert "/opt/venv" not in serialized
    assert "/private/value" not in serialized


def test_typed_rejection_does_not_promote_implicit_exception_context() -> None:
    try:
        try:
            raise AttributeError("private dependency text")
        except AttributeError:
            raise LIBEROPandaInstrumentationError(
                "self-contained MissionOS refusal",
                rejection_reason=(
                    LIBEROPandaInstrumentationRejectionReason
                    .POLICY_RESPONSE_ACTION_FIELDS_MISSING
                ),
            )
    except LIBEROPandaInstrumentationError as error:
        report = _failure_report(
            phase="official_rollout",
            error=error,
        )

    assert "exception_fingerprint" not in report["failure"]
    assert report["failure"]["rejection_reason"] == (
        "policy_response_action_fields_missing"
    )


class _FakeSignalModule:
    SIGALRM = 14
    ITIMER_REAL = 0

    def __init__(self) -> None:
        self.previous_handler = object()
        self.handler = None
        self.timer_calls = []
        self.signal_calls = []

    def getitimer(self, which):
        assert which == self.ITIMER_REAL
        return (0.0, 0.0)

    def getsignal(self, signum):
        assert signum == self.SIGALRM
        return self.previous_handler

    def signal(self, signum, handler):
        assert signum == self.SIGALRM
        self.handler = handler
        self.signal_calls.append(handler)

    def setitimer(self, which, seconds):
        assert which == self.ITIMER_REAL
        self.timer_calls.append(seconds)


def test_rollout_deadline_interrupts_and_restores_process_timer() -> None:
    signal_module = _FakeSignalModule()

    try:
        with _rollout_deadline(12.5, signal_module=signal_module):
            signal_module.handler(signal_module.SIGALRM, None)
    except LIBEROPandaInstrumentationError as error:
        report = _failure_report(
            phase="official_rollout",
            error=error,
            maximum_rollout_elapsed_seconds=12.5,
            rollout_elapsed_seconds=12.75,
        )
    else:
        raise AssertionError("the elapsed-time deadline did not interrupt")

    assert signal_module.timer_calls == [12.5, 0.0]
    assert signal_module.signal_calls[-1] is signal_module.previous_handler
    assert report["failure"] == {
        "phase": "official_rollout",
        "error_type": "LIBEROPandaInstrumentationError",
        "rejection_reason": "rollout_deadline_exceeded",
        "error_message_included": False,
    }
    assert "exception_fingerprint" not in report["failure"]
    assert report["diagnostic_run_limit"] == {
        "maximum_rollout_elapsed_seconds": 12.5,
        "limit_source": "operator_invocation",
        "outcome_claim_effect": "none",
        "elapsed_before_failure_seconds": 12.75,
        "elapsed_clock_basis": "process_monotonic",
    }
    assert report["claim_boundary"]["physical_execution_invoked"] is False


@pytest.mark.parametrize(
    "value",
    ("nan", "inf", "-inf", "0", "-1", True, False),
)
def test_rollout_deadline_cli_rejects_non_positive_or_non_finite_values(
    value: object,
) -> None:
    with pytest.raises(
        argparse.ArgumentTypeError,
        match="positive finite",
    ):
        _positive_finite_seconds(value)


def test_failure_report_normalizes_python_dependency_location() -> None:
    filename = (
        "/opt/venv/lib/python3.11/site-packages/"
        "robosuite/environments/base.py"
    )
    try:
        exec(compile("raise AttributeError('private value')", filename, "exec"))
    except AttributeError as error:
        report = _failure_report(
            phase="official_rollout",
            error=error,
        )

    assert report["failure"]["exception_fingerprint"] == {
        "innermost_frame_repo": "python_dependency",
        "innermost_frame_package": "robosuite",
        "innermost_frame_path": "robosuite/environments/base.py",
        "innermost_frame_lineno": 1,
    }
    assert "/opt/venv" not in json.dumps(report)
    assert "private value" not in json.dumps(report)


@pytest.mark.parametrize(
    "relative_filename",
    (
        "robosuite/../../private/operator.py",
        "../private/operator.py",
    ),
)
def test_failure_report_rejects_dependency_path_traversal(
    relative_filename: str,
) -> None:
    filename = (
        "/opt/venv/lib/python3.11/site-packages/"
        f"{relative_filename}"
    )
    try:
        exec(compile("raise AttributeError('private value')", filename, "exec"))
    except AttributeError as error:
        report = _failure_report(
            phase="official_rollout",
            error=error,
        )

    assert report["failure"]["exception_fingerprint"] == {
        "innermost_frame_repo": "unknown",
        "innermost_frame_path": None,
        "innermost_frame_lineno": 1,
    }
    serialized = json.dumps(report)
    assert "/opt/venv" not in serialized
    assert "private value" not in serialized
    assert "../" not in serialized


def test_failure_report_uses_innermost_chained_exception_location(
    tmp_path,
) -> None:
    isaac_root = tmp_path / "Isaac-GR00T"
    libero_path = (
        isaac_root
        / "external_dependencies"
        / "LIBERO"
        / "libero"
        / "libero"
        / "envs"
        / "env_wrapper.py"
    )
    try:
        try:
            exec(
                compile(
                    "raise AttributeError('private value')",
                    str(libero_path),
                    "exec",
                )
            )
        except AttributeError as error:
            raise RuntimeError("wrapper text") from error
    except RuntimeError as error:
        report = _failure_report(
            phase="official_rollout",
            error=error,
            isaac_groot_root=isaac_root,
        )

    assert report["failure"]["exception_fingerprint"] == {
        "innermost_frame_repo": "libero",
        "innermost_frame_path": "libero/libero/envs/env_wrapper.py",
        "innermost_frame_lineno": 1,
    }
    assert str(tmp_path) not in json.dumps(report)


def test_failure_report_does_not_publish_unclassified_local_path() -> None:
    try:
        exec(
            compile(
                "raise RuntimeError('private value')",
                "/private/user/secret.py",
                "exec",
            )
        )
    except RuntimeError as error:
        report = _failure_report(
            phase="official_rollout",
            error=error,
        )

    assert report["failure"]["exception_fingerprint"] == {
        "innermost_frame_repo": "unknown",
        "innermost_frame_path": None,
        "innermost_frame_lineno": 1,
    }
    assert "/private/user" not in json.dumps(report)
