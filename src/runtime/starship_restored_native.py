"""Explicit worker composition of ground flight, judgments and actual return.

This function does not register or authorize a cohort. The source-bound outer
runner owns its opt-in, subprocess, deadlines, resource limits, host/Gateway
and saved-only verification. No provider, signing key or operator token belongs
in this worker. A fixture host exercises the same pipe and plant boundaries;
it cannot establish that hosted inference occurred.
"""
from copy import deepcopy
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re

from .starship_inflight_runtime import ApprovalPipeReader, ApprovalPipeWriter, _context
from .starship_restored_contract import _manifest
from .starship_restored_coordinator import RestoredReturnCoordinator
from .starship_restored_decision_pipe import RestoredDecisionClient
from .starship_restored_deployment import RestoredDeploymentJudgments
from .starship_restored_integration import RestoredIntegrationPolicy, restored_integration
from .starship_restored_prefix import RestoredPrefixRuntime, restored_prefix_mission, _native
from .starship_restored_operations import RegisteredRestoredOperations


def _wire(value):
    return json.dumps(_native(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(value):
    return sha256(_wire(value)).hexdigest()


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _empty_notice(_value):
    return ""


def run_restored_native_worker(profile, *, profile_raw_bytes, expected_source_sha256,
        ascent_settings, control_settings, return_sites, splashdown_goal,
        context, plan_sha256, case_id, mode, output_dir, integration_policy,
        decision_request_writer, decision_response_reader, ready_writer, ready_reader,
        approval_request_writer, approval_grant_reader, telemetry_writer,
        sites=None, notice_resources=None, notice_generations=None,
        deployment_notice=_empty_notice, notice_summary=_empty_notice, registered_operations=None,
        on_tick=None, on_return_snapshot=None, return_context_projection=False,
        return_review_policy=None):
    """Run once from ground; retain the prefix even if return cannot be built.

    Callbacks receive detached observations and never supply plant state. The
    complete integration receipt is produced only after prefix and actual
    continuation exit their common policy2 scope. Forecast subprocesses produce
    their own separate explicit numerical receipts.
    """
    _manifest(expected_source_sha256)
    _context(context)
    if return_review_policy is not None:
        from .starship_restored_return_review import validate_policy
        return_review_policy = validate_policy(return_review_policy)
        _require(expected_source_sha256.get("src/runtime/starship_restored_return_review.py")
            == sha256(Path(__file__).with_name("starship_restored_return_review.py").read_bytes()).hexdigest(),
            "restored_native_return_review_source_opt_in_required")
    _require(type(return_context_projection) is bool and (not return_context_projection
        or "src/runtime/starship_restored_return_context.py" in expected_source_sha256),
        "restored_native_return_context_source_opt_in_required")
    _require(type(profile_raw_bytes) is bytes and _wire(json.loads(profile_raw_bytes)) == _wire(profile)
        and "src/runtime/starship_restored_native.py" in expected_source_sha256
        and type(plan_sha256) is str and re.fullmatch(r"[a-f0-9]{64}", plan_sha256)
        and type(case_id) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", case_id)
        and mode in ("live", "fixture") and type(integration_policy) is RestoredIntegrationPolicy
        and integration_policy.subdivisions == 2,
        "restored_native_source_profile_scope_or_policy_changed")
    _require(all(isinstance(value, ApprovalPipeWriter) for value in
            (decision_request_writer, ready_writer, approval_request_writer, telemetry_writer))
        and all(isinstance(value, ApprovalPipeReader) for value in
            (decision_response_reader, ready_reader, approval_grant_reader))
        and (on_tick is None or callable(on_tick))
        and (on_return_snapshot is None or callable(on_return_snapshot)),
        "restored_native_requires_inherited_pipes_and_observers")
    if registered_operations is not None:
        _require(type(registered_operations) is RegisteredRestoredOperations
            and sites is notice_resources is notice_generations is None
            and deployment_notice is _empty_notice and notice_summary is _empty_notice
            and "src/runtime/starship_restored_operations.py" in expected_source_sha256,
            "restored_native_registered_operations_cannot_mix_legacy_tools")
        deployment_notice = registered_operations.deployment_notice
        sites, notice_generations = registered_operations.sites, registered_operations.notice_generations
    else:
        _require(sites is not None and callable(notice_resources) and notice_generations is not None
            and callable(deployment_notice) and callable(notice_summary),
            "restored_native_operation_tools_required")
    output = Path(output_dir).absolute()
    _require(output.is_dir() and not output.is_symlink() and not (output/"prefix.json.gz").exists()
        and not (output/"governance").exists(), "restored_native_exclusive_worker_output_required")
    client = RestoredDecisionClient(context=context, plan_sha256=plan_sha256,
        source_sha256=_hash(expected_source_sha256), request_writer=decision_request_writer,
        response_reader=decision_response_reader,
        **({"return_review_policy": return_review_policy} if return_review_policy is not None else {}))
    deployment = RestoredDeploymentJudgments(client,
        scope_sha256=_hash({"context": context, "plan_sha256": plan_sha256,
            "source_sha256": expected_source_sha256, "phase": "deployment"}),
        notice=deployment_notice, maximum_existing_step_s=max(
            profile["integration"]["powered_dt_s"], profile["integration"]["coast_dt_s"]))
    coordinator = None
    with restored_integration(integration_policy) as numerical:
        runtime = RestoredPrefixRuntime(profile_raw_bytes, expected_source_sha256,
            ascent_settings, control_settings, 7500., deployment)
        prefix = restored_prefix_mission(profile, runtime, return_sites=return_sites,
            splashdown_goal=splashdown_goal, on_tick=on_tick)
        raw = _wire(prefix)
        with (output/"prefix.json.gz").open("xb") as handle:
            handle.write(gzip.compress(raw, mtime=0))
        _require(gzip.decompress((output/"prefix.json.gz").read_bytes()) == raw,
            "restored_native_saved_prefix_restore_mismatch")
        prefix = json.loads(raw)
        deployment_record = deployment.finalize_prefix(prefix)
        if prefix["restored_prefix"]["closed"] is False:
            # A failed ascent before any judgment remains a valid saved physical
            # negative. Partial model work cannot be discarded into this scope.
            evidence = client.record()
            _require(evidence["pending_request"] is None and not evidence["results"] and not evidence["events"],
                "restored_native_unclosed_prefix_has_judgment_work")
        else:
            _require(prefix["restored_prefix"]["closed"] is True,
                "restored_native_requires_closed_actual_inventory")
            actual_origin = runtime.origin()
            if registered_operations is not None:
                operation_tool = registered_operations.bind(actual_origin)
                notice_resources, notice_summary = operation_tool, operation_tool.notice_summary
            coordinator = RestoredReturnCoordinator(actual_origin, profile_raw_bytes=profile_raw_bytes,
                expected_source_sha256=expected_source_sha256, integration_policy=integration_policy,
                context=context, plan_sha256=plan_sha256, output_dir=output/"governance",
                decision_client=client, ready_writer=ready_writer, ready_reader=ready_reader,
                approval_request_writer=approval_request_writer, approval_grant_reader=approval_grant_reader,
                telemetry_writer=telemetry_writer, sites=sites, notice_resources=notice_resources,
                notice_generations=notice_generations, deployment_record=deployment_record,
                notice_summary=notice_summary, return_context_projection=return_context_projection,
                **({"return_review_policy": return_review_policy} if return_review_policy is not None else {}))
            try:
                coordinator.start()
                while coordinator.result is None:
                    snapshot = coordinator.tick()
                    if on_return_snapshot is not None:
                        on_return_snapshot(deepcopy(snapshot))
            finally:
                # Forecast workers own sessions, so killing only the outer worker
                # is insufficient after an observer/transport boundary exception.
                coordinator.close()
    continuation = None if coordinator is None else coordinator.result.record()
    value = {"schema": "missionos.starship_restored_native_mission.v1", "execution": {
            "schema": "missionos.starship_restored_native_execution.v1", "context": deepcopy(context),
            "plan_sha256": plan_sha256, "case_id": case_id, "source_sha256": deepcopy(expected_source_sha256),
            "profile_sha256": sha256(profile_raw_bytes).hexdigest(),
            "settings_sha256": _hash(ascent_settings.record()), "control_settings_sha256": _hash(control_settings.record()),
            "prefix_horizon_s": 7500., "total_horizon_s": 16000.,
            "initial_return_candidate_id": "nominal", "integration_policy": integration_policy.record(), "mode": mode},
        "prefix": prefix, "return": continuation, "numerical_integration": numerical.record(),
        "governance": None if coordinator is None else coordinator.record()}
    if return_review_policy is not None:
        from .starship_restored_return_review import policy_digest
        value["execution"].update(return_review_policy=deepcopy(return_review_policy),
            return_review_policy_sha256=policy_digest(return_review_policy))
    return value
