from __future__ import annotations

from copy import deepcopy

import numpy as np
import pytest

from missionos_core import canonical_sha256
from scripts import run_groot_libero_same_world_repair as live_runner
from src.gateway.missionos_dispatch_runtime import DispatchAuthorityTable
from src.runtime import groot_libero_same_world_repair as same_world_repair
from src.runtime.groot_libero_same_world_repair import (
    FRAME_CAPTURE_AUTHORITY,
    FRAME_CAPTURE_SCHEMA_VERSION,
    PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
    STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE,
    STATE_CONTINUITY_LIVE_SAME_WORLD,
    approve_same_world_repair,
    build_exact_repair_instruction_payload,
    build_same_world_repair_dispatch,
    build_same_world_repair_proposal,
    normalize_frame_capture,
    normalize_preservation_step_trace,
    preserved_object_names,
    run_same_world_repair,
    verify_exact_repair_instruction_payload,
)
from src.runtime.libero_panda_predicate_package import (
    LIBERO_PANDA_SCENE8_ENVIRONMENT,
)


def _vector(*, first: bool = True, second: bool = False, stove: bool = True) -> list[dict]:
    predicates = [
        ("on", ["moka_pot_1", "flat_stove_1_cook_region"], first),
        ("on", ["moka_pot_2", "flat_stove_1_cook_region"], second),
        ("turnon", ["flat_stove_1"], stove),
    ]
    return [
        {
            "predicate_index": index,
            "predicate_id": canonical_sha256(
                {
                    "predicate_index": index,
                    "predicate_name": name,
                    "arguments": arguments,
                }
            ),
            "predicate_name": name,
            "arguments": arguments,
            "satisfied": satisfied,
        }
        for index, (name, arguments, satisfied) in enumerate(predicates)
    ]


def _authorization(
    *,
    maximum_repair_chunks: int = 3,
    repair_instruction_variant: str = "semantic_preserve",
):
    proposal = build_same_world_repair_proposal(
        environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        environment_session_id="libero-world:test",
        source_contract_sha256="a" * 64,
        source_goal_predicates=_vector(),
        reset_count=1,
        maximum_repair_chunks=maximum_repair_chunks,
        repair_instruction_variant=repair_instruction_variant,
        proposal_id="proposal:test",
        proposed_at="2026-08-12T00:00:00+00:00",
    )
    approval = approve_same_world_repair(
        proposal=proposal,
        operator_approval_ref="operator:test",
        approval_id="approval:test",
        approved_at="2026-08-12T00:01:00+00:00",
    )
    dispatch = build_same_world_repair_dispatch(
        proposal=proposal,
        approval=approval,
        dispatch_ref="dispatch:test",
        created_at="2026-08-12T00:02:00+00:00",
    )
    return proposal, approval, dispatch


def _invocation(proposal: dict, chunk_index: int = 0) -> dict:
    instruction = proposal["repair_instruction"]
    return {
        "model_runtime_invoked": True,
        "repair_instruction_sha256": proposal["repair_instruction_sha256"],
        "repair_instruction_payload_exact_match": True,
        "repair_instruction_payload_sha256": proposal["repair_instruction_sha256"],
        "repair_instruction_payload_length": len(instruction),
        "repair_instruction_payload_kind": "numpy.ndarray",
        "repair_instruction_payload_dtype": f"<U{len(instruction)}",
        "repair_instruction_payload_shape": [1],
        "policy_request_sha256": f"request:{chunk_index}",
        "policy_response_sha256": f"response:{chunk_index}",
    }


def _frame(*, step: int, sha256: str | None = None) -> dict:
    return {
        "schema_version": FRAME_CAPTURE_SCHEMA_VERSION,
        "authority": FRAME_CAPTURE_AUTHORITY,
        "status": "captured",
        "cameras": [
            {
                "observation_key": "video.image",
                "image_sha256": sha256 or f"{step:064x}",
                "artifact_relative_path": f"chunk0000/step{step:02d}_image.png",
                "encoding": "png",
                "height_pixels": 256,
                "width_pixels": 256,
                "channels": 3,
            }
        ],
    }


MOKA_POT_1_REST_POSITION = [0.1, 0.2, 0.3]


def _step_trace(
    *,
    chunk_index: int,
    vectors: list[list[dict]],
    frames: bool | str = False,
    lift_from_step: int | None = None,
    lift_metres: float = 0.011,
    lift_contact: bool = True,
) -> list[dict]:
    if len(vectors) == 1:
        vectors = [deepcopy(vectors[0]) for _ in range(8)]
    assert len(vectors) == 8
    trace = []
    for action_step_index, vector in enumerate(vectors):
        witnesses = {}
        for predicate_index, object_name in enumerate(("moka_pot_1", "moka_pot_2")):
            on_stove = vector[predicate_index]["satisfied"]
            half_extent = [0.075, 0.075, 0.0025]
            local_delta = [0.0 if on_stove else 0.085, 0.0, 0.02]
            margins = {
                "x": half_extent[0] - abs(local_delta[0]),
                "y": half_extent[1] - abs(local_delta[1]),
                "z_lower": local_delta[2] - (half_extent[2] - 0.005),
                "z_upper": (half_extent[2] + 0.10) - local_delta[2],
            }
            lifted = (
                lift_from_step is not None
                and object_name == "moka_pot_1"
                and action_step_index >= lift_from_step
            )
            position = [0.1 + predicate_index, 0.2, 0.3]
            if lifted:
                position = [position[0], position[1], position[2] + lift_metres]
            witnesses[object_name] = {
                "object_name": object_name,
                "position_metres": position,
                "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
                "linear_velocity_metres_per_second": [0.0, 0.0, 0.0],
                "angular_velocity_radians_per_second": [0.0, 0.0, 0.0],
                "step_translation_distance_metres": 0.001,
                "end_effector_distance_metres": 0.2,
                "gripper_contact_observed": bool(lifted and lift_contact),
                "stove_region_witness": {
                    "region_name": "flat_stove_1_cook_region",
                    "local_delta_metres": local_delta,
                    "half_extent_metres": half_extent,
                    "axis_margins_metres": margins,
                    "inside_under_region": on_stove,
                    "stove_parent_contact_observed": True,
                    "on_predicate_witness": on_stove,
                },
            }
        conjunction = all(item["satisfied"] for item in vector)
        global_index = chunk_index * 8 + action_step_index
        trace.append(
            {
                "schema_version": PRESERVATION_STEP_TRACE_SCHEMA_VERSION,
                "chunk_index": chunk_index,
                "action_step_index": action_step_index,
                "action_step_number": action_step_index + 1,
                "global_repair_step_index": global_index,
                "global_repair_step_number": global_index + 1,
                "action_step_sha256": f"action:{chunk_index}:{action_step_index}",
                "goal_predicate_observations": deepcopy(vector),
                "goal_predicate_vector_sha256": canonical_sha256(
                    {"goal_predicate_observations": vector}
                ),
                "official_predicate_conjunction": conjunction,
                "official_predicate_result": conjunction,
                "conjunction_matches_official_result": True,
                "object_witnesses": witnesses,
                **(
                    {}
                    if frames is False
                    else {
                        "frame_capture": (
                            {
                                "schema_version": FRAME_CAPTURE_SCHEMA_VERSION,
                                "authority": FRAME_CAPTURE_AUTHORITY,
                                "status": "capture_failed",
                                "cameras": [],
                                "failure_code": "render_unavailable",
                            }
                            if frames == "failed"
                            else _frame(
                                step=action_step_index,
                                sha256=(
                                    f"{action_step_index + 900:064x}"
                                    if frames == "alternate"
                                    else None
                                ),
                            )
                        )
                    }
                ),
            }
        )
    return trace


def test_exact_instruction_payload_expands_fixed_width_unicode() -> None:
    original = np.asarray(["put both moka pots on the stove"])
    instruction = (
        "Place the second moka pot on the stove. Keep the first moka pot on the "
        "stove and keep the stove turned on."
    )

    payload, evidence = build_exact_repair_instruction_payload(
        current_language=original,
        instruction=instruction,
    )

    assert original.dtype == np.dtype("<U31")
    assert payload[0] == instruction
    assert evidence["repair_instruction_payload_exact_match"] is True
    assert evidence["repair_instruction_payload_length"] == 106
    assert evidence["repair_instruction_payload_sha256"] == canonical_sha256(
        {"repair_instruction": instruction}
    )


def test_truncated_instruction_payload_fails_readback() -> None:
    instruction = (
        "Place the second moka pot on the stove. Keep the first moka pot on the "
        "stove and keep the stove turned on."
    )
    truncated = np.asarray([instruction], dtype="<U31")

    with pytest.raises(ValueError, match="payload_exact_match_failed"):
        verify_exact_repair_instruction_payload(
            payload=truncated,
            expected_instruction=instruction,
        )


def test_proposal_binds_partial_vector_instruction_budget_and_same_world() -> None:
    proposal, approval, dispatch = _authorization(maximum_repair_chunks=90)

    assert proposal["proposal_status"] == "awaiting_operator_approval"
    assert proposal["repair_instruction"] == (
        "Place the second moka pot on the stove. Keep the first moka pot on the "
        "stove and keep the stove turned on."
    )
    assert len(proposal["preserve_predicate_ids"]) == 2
    assert len(proposal["target_predicate_ids"]) == 1
    assert proposal["repair_contract"]["maximum_repair_steps"] == 720
    assert proposal["repair_contract"]["verify_after_each_chunk"] is True
    assert proposal["repair_instruction_variant"] == "semantic_preserve"
    assert proposal["repair_contract"]["repair_instruction_variant"] == "semantic_preserve"
    assert approval["proposal_sha256"] == proposal["proposal_sha256"]
    assert approval["repair_instruction_sha256"] == proposal["repair_instruction_sha256"]
    assert approval["single_use"] is True
    assert dispatch["approval_sha256"] == approval["approval_sha256"]
    assert dispatch["repair_contract_sha256"] == proposal["repair_contract_sha256"]
    assert approval["repair_instruction_variant"] == "semantic_preserve"
    assert dispatch["repair_instruction_variant"] == "semantic_preserve"


@pytest.mark.parametrize(
    ("variant", "instruction"),
    [
        (
            "semantic_preserve",
            "Place the second moka pot on the stove. Keep the first moka pot on the "
            "stove and keep the stove turned on.",
        ),
        ("original_task", "put both moka pots on the stove"),
        ("short_target", "put the second moka pot on the stove"),
        ("cached_singular_task", "turn on the stove and put the moka pot on it"),
    ],
)
def test_proposal_binds_fixed_instruction_ablation_variant(
    variant: str,
    instruction: str,
) -> None:
    proposal = build_same_world_repair_proposal(
        environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        environment_session_id=f"libero-world:{variant}",
        source_contract_sha256="a" * 64,
        source_goal_predicates=_vector(),
        reset_count=1,
        repair_instruction_variant=variant,
        proposal_id=f"proposal:{variant}",
        proposed_at="2026-08-14T00:00:00+00:00",
    )

    assert proposal["repair_instruction_variant"] == variant
    assert proposal["repair_instruction"] == instruction
    assert proposal["repair_contract"]["repair_instruction_variant"] == variant
    assert proposal["repair_instruction_sha256"] == canonical_sha256(
        {"repair_instruction": instruction}
    )


def test_instruction_ablation_variants_change_contract_and_proposal_digests() -> None:
    proposals = [
        build_same_world_repair_proposal(
            environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
            environment_session_id="libero-world:ablation",
            source_contract_sha256="a" * 64,
            source_goal_predicates=_vector(),
            reset_count=1,
            repair_instruction_variant=variant,
            proposal_id="proposal:ablation",
            proposed_at="2026-08-14T00:00:00+00:00",
        )
        for variant in (
            "semantic_preserve",
            "original_task",
            "short_target",
            "cached_singular_task",
        )
    ]

    assert len({item["repair_contract_sha256"] for item in proposals}) == 4
    assert len({item["proposal_sha256"] for item in proposals}) == 4
    assert len({item["repair_instruction_sha256"] for item in proposals}) == 4

    fixed_materials = []
    for proposal in proposals:
        material = deepcopy(proposal["repair_contract"])
        material.pop("repair_instruction_variant")
        material.pop("repair_instruction_sha256")
        material["instruction_ablation"].pop("variant")
        material["instruction_ablation"].pop("target_specific_instruction")
        material["repair_intent_selection"].pop("repair_instruction_variant")
        material["repair_intent_selection"].pop("repair_instruction")
        material["repair_intent_selection"].pop("repair_instruction_sha256")
        fixed_materials.append(material)
    assert fixed_materials[0] == fixed_materials[1] == fixed_materials[2]
    assert proposals[0]["repair_contract"]["instruction_ablation"] == {
        "controlled_variable": "repair_instruction",
        "variant": "semantic_preserve",
        "target_specific_instruction": True,
        "fixed_comparison_metrics": [
            "target_minimum_end_effector_distance_metres",
            "target_gripper_contact_steps",
            "target_maximum_displacement_metres",
            "protected_object_gripper_contact_steps",
            "protected_object_maximum_displacement_metres",
            "chunk_predicate_timeline",
            "terminal_status",
        ],
        "root_cause_established": False,
    }


def test_proposal_rejects_unregistered_instruction_ablation_variant() -> None:
    with pytest.raises(ValueError, match="instruction_variant_not_supported"):
        build_same_world_repair_proposal(
            environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
            environment_session_id="libero-world:invalid-variant",
            source_contract_sha256="a" * 64,
            source_goal_predicates=_vector(),
            reset_count=1,
            repair_instruction_variant="operator_free_text",
        )


@pytest.mark.parametrize("variant", ["original_task", "short_target"])
def test_live_cli_accepts_only_catalogued_instruction_ablation_variants(variant: str) -> None:
    args = live_runner._parser().parse_args(
        [
            "--model-path",
            "/tmp/model",
            "--output",
            "/tmp/result.json",
            "--dispatch-state-path",
            "/tmp/dispatch.json",
            "--operator-approval-ref",
            "operator:test",
            "--repair-instruction-variant",
            variant,
        ]
    )
    assert args.repair_instruction_variant == variant


def test_diagnostic_handoff_snapshot_round_trip_is_digest_bound(tmp_path) -> None:
    snapshot = tmp_path / "handoff.npz"
    state = np.asarray([1.0, 2.0, 3.0], dtype=np.float64)
    written = live_runner._write_diagnostic_handoff_snapshot(
        path=snapshot,
        simulator_state=state,
        metadata={
            "environment": LIBERO_PANDA_SCENE8_ENVIRONMENT,
            "process_seed": 14,
            "source_contract_sha256": "a" * 64,
            "source_steps_executed": 720,
            "source_goal_predicate_vector_sha256": "b" * 64,
            "source_object_poses": {"moka_pot_1": [1.0, 2.0, 3.0]},
        },
    )

    restored_state, restored = live_runner._read_diagnostic_handoff_snapshot(snapshot)

    assert np.array_equal(restored_state, state)
    assert restored["snapshot_artifact_sha256"] == written["snapshot_artifact_sha256"]
    assert restored["semantic_repair_claim_eligible"] is False
    assert restored["authority"] == "diagnostic_only"
    assert restored["local_path_recorded"] is False


def test_diagnostic_handoff_snapshot_rejects_state_digest_mismatch(tmp_path) -> None:
    snapshot = tmp_path / "handoff.npz"
    np.savez_compressed(
        snapshot,
        simulator_state=np.asarray([1.0, 2.0], dtype=np.float64),
        metadata_json=np.asarray(
            '{"schema_version":"missionos_groot_libero_diagnostic_handoff_snapshot.v1",'
            '"authority":"diagnostic_only",'
            '"semantic_repair_claim_eligible":false,'
            '"simulator_state_sha256":"' + "0" * 64 + '"}'
        ),
    )

    with pytest.raises(ValueError, match="simulator_state_digest_mismatch"):
        live_runner._read_diagnostic_handoff_snapshot(snapshot)


def test_diagnostic_clone_proposal_is_bound_but_never_claim_eligible() -> None:
    live = build_same_world_repair_proposal(
        environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        environment_session_id="libero-world:live",
        source_contract_sha256="a" * 64,
        source_goal_predicates=_vector(),
        reset_count=1,
        proposal_id="proposal:live",
        proposed_at="2026-08-14T00:00:00+00:00",
    )
    cloned = build_same_world_repair_proposal(
        environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        environment_session_id="libero-world:clone",
        source_contract_sha256="a" * 64,
        source_goal_predicates=_vector(),
        reset_count=1,
        proposal_id="proposal:clone",
        proposed_at="2026-08-14T00:00:00+00:00",
        state_continuity_basis=STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE,
        diagnostic_handoff_snapshot_sha256="c" * 64,
    )

    assert live["state_continuity_basis"] == STATE_CONTINUITY_LIVE_SAME_WORLD
    assert live["semantic_repair_claim_eligible"] is True
    assert cloned["same_world_state_observed"] is False
    assert cloned["diagnostic_cloned_state_observed"] is True
    assert cloned["semantic_repair_claim_eligible"] is False
    assert cloned["repair_contract"]["diagnostic_handoff_snapshot_sha256"] == "c" * 64
    assert cloned["repair_contract_sha256"] != live["repair_contract_sha256"]
    successful_result = {
        "predicate_improvement_observed": True,
        "task_completion_claimed": True,
    }
    assert live_runner._semantic_repair_established(proposal=live, repair_result=successful_result)
    assert not live_runner._semantic_repair_established(
        proposal=cloned, repair_result=successful_result
    )
    assert not live_runner._task_completion_claimed(
        proposal=cloned, repair_result=successful_result
    )


def test_diagnostic_continuity_inputs_fail_closed() -> None:
    common = {
        "environment": LIBERO_PANDA_SCENE8_ENVIRONMENT,
        "environment_session_id": "libero-world:test",
        "source_contract_sha256": "a" * 64,
        "source_goal_predicates": _vector(),
        "reset_count": 1,
    }
    with pytest.raises(ValueError, match="live_same_world_cannot_bind"):
        build_same_world_repair_proposal(
            **common,
            diagnostic_handoff_snapshot_sha256="c" * 64,
        )
    with pytest.raises(ValueError, match="snapshot_sha256_invalid"):
        build_same_world_repair_proposal(
            **common,
            state_continuity_basis=STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE,
        )


def test_diagnostic_clone_satisfaction_is_observed_but_never_claimed(tmp_path) -> None:
    proposal = build_same_world_repair_proposal(
        environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        environment_session_id="libero-world:diagnostic-clone",
        source_contract_sha256="a" * 64,
        source_goal_predicates=_vector(),
        reset_count=1,
        maximum_repair_chunks=1,
        proposal_id="proposal:diagnostic-clone",
        proposed_at="2026-08-14T00:00:00+00:00",
        state_continuity_basis=STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE,
        diagnostic_handoff_snapshot_sha256="c" * 64,
    )
    approval = approve_same_world_repair(
        proposal=proposal,
        operator_approval_ref="operator:diagnostic-clone",
        approval_id="approval:diagnostic-clone",
        approved_at="2026-08-14T00:01:00+00:00",
    )
    dispatch = build_same_world_repair_dispatch(
        proposal=proposal,
        approval=approval,
        dispatch_ref="dispatch:diagnostic-clone",
        created_at="2026-08-14T00:02:00+00:00",
    )
    current_vector = _vector()
    satisfied_vector = _vector(second=True)

    def apply_action_chunk(action, chunk_index):
        current_vector[:] = satisfied_vector
        return {"version": 1}, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": True,
            "action_chunk_sha256": "action:diagnostic-clone",
            "official_predicate_result": True,
            "preservation_step_trace": _step_trace(
                chunk_index=chunk_index,
                vectors=[satisfied_vector],
            ),
        }

    ledger = DispatchAuthorityTable(tmp_path / "dispatch.json")
    result = run_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=ledger,
        initial_observation={"version": 0},
        invoke_model=lambda observation, instruction, chunk_index: (
            {"chunk": chunk_index},
            _invocation(proposal),
        ),
        apply_action_chunk=apply_action_chunk,
        observe_goal_predicates=lambda: deepcopy(current_vector),
        observed_reset_count=lambda: 1,
        observed_state_continuity_basis=STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE,
    )

    assert result["status"] == "satisfied_diagnostic_observation"
    assert result["predicate_conjunction_observed"] is True
    assert result["predicate_improvement_observed"] is True
    assert result["task_completion_claimed"] is False
    assert result["single_reset_observed"] is True
    assert result["same_world_state_preserved"] is False
    assert result["semantic_repair_claim_eligible"] is False
    receipt = ledger.lookup_dispatch_ref(dispatch["dispatch_ref"])["receipt"]
    assert receipt["completion_claimed"] is False
    assert receipt["verifier_passed"] is True
    sender_receipt = receipt["receipt"]
    assert sender_receipt["status"] == "satisfied_diagnostic_observation"
    assert sender_receipt["verifier_passed"] is True
    assert sender_receipt["completion_claimed"] is False


def test_execution_rejects_declared_live_basis_for_observed_clone(tmp_path) -> None:
    proposal, approval, dispatch = _authorization(maximum_repair_chunks=1)

    with pytest.raises(ValueError, match="observed_state_continuity_basis_mismatch"):
        run_same_world_repair(
            proposal=proposal,
            approval=approval,
            dispatch=dispatch,
            dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch.json"),
            initial_observation={"version": 0},
            invoke_model=lambda observation, instruction, chunk_index: (
                {"chunk": chunk_index},
                _invocation(proposal),
            ),
            apply_action_chunk=lambda action, chunk_index: ({"version": 1}, {}),
            observe_goal_predicates=lambda: _vector(),
            observed_reset_count=lambda: 1,
            observed_state_continuity_basis=(STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE),
        )


def test_approval_rejects_rehashed_inconsistent_clone_binding() -> None:
    proposal = build_same_world_repair_proposal(
        environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        environment_session_id="libero-world:diagnostic-clone",
        source_contract_sha256="a" * 64,
        source_goal_predicates=_vector(),
        reset_count=1,
        state_continuity_basis=STATE_CONTINUITY_DIAGNOSTIC_MUJOCO_CLONE,
        diagnostic_handoff_snapshot_sha256="c" * 64,
    )
    forged = deepcopy(proposal)
    forged["semantic_repair_claim_eligible"] = True
    forged_material = {key: value for key, value in forged.items() if key != "proposal_sha256"}
    forged["proposal_sha256"] = canonical_sha256(forged_material)

    with pytest.raises(ValueError, match="claim_eligibility_mismatch"):
        approve_same_world_repair(
            proposal=forged,
            operator_approval_ref="operator:forged-clone",
        )


def test_live_cli_parses_diagnostic_handoff_controls(tmp_path) -> None:
    args = live_runner._parser().parse_args(
        [
            "--model-path",
            "/tmp/model",
            "--output",
            "/tmp/result.json",
            "--dispatch-state-path",
            "/tmp/dispatch.json",
            "--operator-approval-ref",
            "operator:test",
            "--diagnostic-handoff-state-out",
            str(tmp_path / "handoff.npz"),
            "--diagnostic-capture-only",
        ]
    )

    assert args.diagnostic_handoff_state_out == tmp_path / "handoff.npz"
    assert args.diagnostic_capture_only is True


def test_live_cli_wires_instruction_variant_into_execution(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    captured: dict = {}

    def fake_execute(**kwargs):
        captured.update(kwargs)
        return {
            "schema_version": "fixture",
            "result": "fixture",
            "claim_boundary": {
                "semantic_repair_established": False,
                "task_completion_claimed": False,
            },
        }

    output = tmp_path / "result.json"
    monkeypatch.setenv(live_runner.OPT_IN_ENV, "1")
    monkeypatch.setattr(live_runner, "execute_same_world_repair", fake_execute)
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_groot_libero_same_world_repair.py",
            "--model-path",
            str(tmp_path / "model"),
            "--output",
            str(output),
            "--dispatch-state-path",
            str(tmp_path / "dispatch.json"),
            "--operator-approval-ref",
            "operator:test",
            "--repair-instruction-variant",
            "short_target",
        ],
    )

    live_runner.main()

    assert captured["repair_instruction_variant"] == "short_target"
    assert output.exists()
    assert '"physical_execution_invoked": false' in capsys.readouterr().out


def test_dispatch_rejects_changed_instruction_ablation_variant() -> None:
    proposal, approval, _ = _authorization()
    changed = dict(approval)
    changed["repair_instruction_variant"] = "original_task"
    changed_material = {key: value for key, value in changed.items() if key != "approval_sha256"}
    changed["approval_sha256"] = canonical_sha256(changed_material)

    with pytest.raises(ValueError, match="instruction_variant_binding_mismatch"):
        build_same_world_repair_dispatch(
            proposal=proposal,
            approval=changed,
            dispatch_ref="dispatch:changed-variant",
        )


def test_approval_rejects_rehashed_free_text_instruction() -> None:
    proposal, _, _ = _authorization()
    changed = deepcopy(proposal)
    changed["repair_instruction"] = "move something somewhere"
    changed["repair_instruction_sha256"] = canonical_sha256(
        {"repair_instruction": changed["repair_instruction"]}
    )
    changed["repair_contract"]["repair_instruction_sha256"] = changed["repair_instruction_sha256"]
    changed["repair_contract_sha256"] = canonical_sha256(changed["repair_contract"])
    changed_material = {key: value for key, value in changed.items() if key != "proposal_sha256"}
    changed["proposal_sha256"] = canonical_sha256(changed_material)

    with pytest.raises(ValueError, match="instruction_catalog_binding_mismatch"):
        approve_same_world_repair(
            proposal=changed,
            operator_approval_ref="operator:forged",
        )


def test_execution_rejects_rehashed_forged_ablation_preregistration(tmp_path) -> None:
    proposal, approval, dispatch = _authorization(repair_instruction_variant="short_target")
    changed = deepcopy(proposal)
    changed["repair_contract"]["instruction_ablation"]["root_cause_established"] = True
    changed["repair_contract_sha256"] = canonical_sha256(changed["repair_contract"])
    changed_material = {key: value for key, value in changed.items() if key != "proposal_sha256"}
    changed["proposal_sha256"] = canonical_sha256(changed_material)

    with pytest.raises(ValueError, match="instruction_ablation_binding_mismatch"):
        run_same_world_repair(
            proposal=changed,
            approval=approval,
            dispatch=dispatch,
            dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch.json"),
            initial_observation={"version": 0},
            invoke_model=lambda observation, instruction, chunk_index: ({}, {}),
            apply_action_chunk=lambda action, chunk_index: ({}, {}),
            observe_goal_predicates=_vector,
            observed_reset_count=lambda: 1,
        )


def test_proposal_supports_live_observed_first_pot_only_repair() -> None:
    proposal = build_same_world_repair_proposal(
        environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        environment_session_id="libero-world:first-pot-unmet",
        source_contract_sha256="b" * 64,
        source_goal_predicates=_vector(first=False, second=True, stove=True),
        reset_count=1,
    )

    assert proposal["repair_instruction"] == (
        "Place the first moka pot on the stove. Keep the second moka pot on the "
        "stove and keep the stove turned on."
    )
    assert proposal["target_predicate_ids"] == [
        proposal["source_goal_predicate_observations"][0]["predicate_id"]
    ]
    assert proposal["preserve_predicate_ids"] == [
        proposal["source_goal_predicate_observations"][1]["predicate_id"],
        proposal["source_goal_predicate_observations"][2]["predicate_id"],
    ]


@pytest.mark.parametrize(
    ("vector", "match"),
    [
        (_vector(first=False, second=False, stove=False), "no_completed_predicate"),
        (_vector(first=True, second=True, stove=True), "no_unmet_predicate"),
        (_vector(first=True, second=True, stove=False), "target_not_supported"),
    ],
)
def test_proposal_rejects_vectors_that_cannot_support_semantic_repair(
    vector: list[dict],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        build_same_world_repair_proposal(
            environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
            environment_session_id="libero-world:test",
            source_contract_sha256="a" * 64,
            source_goal_predicates=vector,
            reset_count=1,
        )


def test_proposal_refuses_a_reset_world() -> None:
    with pytest.raises(ValueError, match="exactly_one_reset"):
        build_same_world_repair_proposal(
            environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
            environment_session_id="libero-world:test",
            source_contract_sha256="a" * 64,
            source_goal_predicates=_vector(),
            reset_count=2,
        )


def test_closed_loop_reobserves_and_stops_on_predicate_improvement(tmp_path) -> None:
    proposal, approval, dispatch = _authorization()
    ledger = DispatchAuthorityTable(tmp_path / "dispatch.json")
    current_vector = _vector()
    invocations: list[tuple[int, str, int]] = []
    applications: list[int] = []

    def invoke_model(observation, instruction, chunk_index):
        invocations.append((observation["version"], instruction, chunk_index))
        return {"chunk": chunk_index}, {
            **_invocation(proposal, chunk_index),
        }

    def apply_action_chunk(action, chunk_index):
        applications.append(action["chunk"])
        trace_vectors = [_vector()]
        if chunk_index == 1:
            current_vector[:] = _vector(second=True)
            trace_vectors = [_vector()] * 7 + [_vector(second=True)]
        return {"version": chunk_index + 1}, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": True,
            "action_chunk_sha256": f"action:{chunk_index}",
            "official_predicate_result": all(item["satisfied"] for item in current_vector),
            "preservation_step_trace": _step_trace(
                chunk_index=chunk_index,
                vectors=trace_vectors,
            ),
        }

    result = run_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=ledger,
        initial_observation={"version": 0},
        invoke_model=invoke_model,
        apply_action_chunk=apply_action_chunk,
        observe_goal_predicates=lambda: deepcopy(current_vector),
        observed_reset_count=lambda: 1,
    )

    assert [item[0] for item in invocations] == [0, 1]
    assert all(item[1] == proposal["repair_instruction"] for item in invocations)
    assert applications == [0, 1]
    assert result["chunks_executed"] == 2
    assert result["status"] == "satisfied"
    assert result["predicate_improvement_observed"] is True
    assert result["repair_instruction_variant"] == "semantic_preserve"
    assert result["task_completion_claimed"] is True
    assert result["same_world_reset_count"] == 1
    assert result["dispatch_receipt_present"] is True
    assert result["additional_attempt_authorized"] is False

    replay = ledger.claim_dispatch_ref(
        dispatch_ref=dispatch["dispatch_ref"],
        request_payload={
            "dispatch_sha256": dispatch["dispatch_sha256"],
            "proposal_sha256": proposal["proposal_sha256"],
            "approval_sha256": approval["approval_sha256"],
            "repair_contract_sha256": proposal["repair_contract_sha256"],
            "repair_instruction_sha256": proposal["repair_instruction_sha256"],
            "repair_instruction_variant": proposal["repair_instruction_variant"],
            "execution_adapter": proposal["execution_adapter"],
            "environment_session_id": proposal["environment_session_id"],
        },
    )
    assert replay["idempotency_status"] == "existing_receipt"
    assert replay["send_permitted"] is False
    receipt = replay["existing_receipt"]
    assert receipt["verifier_passed"] is True
    assert receipt["completion_claimed"] is True
    assert receipt["receipt"]["status"] == "satisfied"
    assert receipt["receipt"]["completion_claimed"] is True


@pytest.mark.parametrize(
    ("variant", "expected_instruction"),
    [
        ("original_task", "put both moka pots on the stove"),
        ("short_target", "put the second moka pot on the stove"),
    ],
)
def test_instruction_ablation_controls_run_end_to_end(
    tmp_path,
    variant: str,
    expected_instruction: str,
) -> None:
    proposal, approval, dispatch = _authorization(
        maximum_repair_chunks=1,
        repair_instruction_variant=variant,
    )
    observed_instructions: list[str] = []
    current_vector = _vector()

    def invoke_model(observation, instruction, chunk_index):
        observed_instructions.append(instruction)
        return {"chunk": chunk_index}, _invocation(proposal, chunk_index)

    def apply_action_chunk(action, chunk_index):
        current_vector[:] = _vector(second=True)
        return {"version": 1}, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": True,
            "action_chunk_sha256": "action:ablation",
            "official_predicate_result": True,
            "preservation_step_trace": _step_trace(
                chunk_index=chunk_index,
                vectors=[_vector()] * 7 + [_vector(second=True)],
            ),
        }

    result = run_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch.json"),
        initial_observation={"version": 0},
        invoke_model=invoke_model,
        apply_action_chunk=apply_action_chunk,
        observe_goal_predicates=lambda: deepcopy(current_vector),
        observed_reset_count=lambda: 1,
    )

    assert observed_instructions == [expected_instruction]
    assert result["repair_instruction_variant"] == variant
    assert result["instruction_ablation"] == proposal["repair_contract"]["instruction_ablation"]
    assert result["status"] == "satisfied"


def test_closed_loop_stops_if_a_completed_predicate_is_lost(tmp_path) -> None:
    proposal, approval, dispatch = _authorization()
    current_vector = _vector()

    def invoke_model(observation, instruction, chunk_index):
        return {"chunk": chunk_index}, {
            **_invocation(proposal),
        }

    def apply_action_chunk(action, chunk_index):
        current_vector[:] = _vector(first=False)
        return {"version": 1}, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": True,
            "action_chunk_sha256": "action:0",
            "official_predicate_result": False,
            "preservation_step_trace": _step_trace(
                chunk_index=chunk_index,
                vectors=[_vector()] * 3 + [_vector(first=False)] * 5,
            ),
        }

    result = run_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch.json"),
        initial_observation={"version": 0},
        invoke_model=invoke_model,
        apply_action_chunk=apply_action_chunk,
        observe_goal_predicates=lambda: deepcopy(current_vector),
        observed_reset_count=lambda: 1,
    )

    assert result["status"] == "stopped_on_preservation_violation"
    assert result["predicate_improvement_observed"] is False
    assert result["task_completion_claimed"] is False
    assert result["chunks_executed"] == 1
    assert result["preservation_violation_localized_to_simulator_step"] is True
    assert result["admitted_steps_executed_after_first_preservation_violation"] == 4
    assert result["first_preservation_violation"] == {
        "chunk_index": 0,
        "action_step_index": 3,
        "action_step_number": 4,
        "global_repair_step_index": 3,
        "global_repair_step_number": 4,
        "predicate_id": proposal["source_goal_predicate_observations"][0]["predicate_id"],
        "predicate_name": "on",
        "arguments": ["moka_pot_1", "flat_stove_1_cook_region"],
        "prior_satisfied": True,
        "current_satisfied": False,
        "observed_failure_mechanism": "object_left_stove_region",
        "object_witness": result["chunk_evidence"][0]["preservation_step_trace"][3][
            "object_witnesses"
        ]["moka_pot_1"],
        "root_cause_claimed": False,
    }


def test_transient_preservation_loss_stops_before_next_chunk(tmp_path) -> None:
    proposal, approval, dispatch = _authorization()
    final_vector = _vector()

    result = run_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch.json"),
        initial_observation={"version": 0},
        invoke_model=lambda observation, instruction, chunk_index: (
            {"chunk": chunk_index},
            _invocation(proposal, chunk_index),
        ),
        apply_action_chunk=lambda action, chunk_index: (
            {"version": 1},
            {
                "simulator_step_return_observed": True,
                "simulator_effect_observed": True,
                "action_chunk_sha256": "action:0",
                "official_predicate_result": False,
                "preservation_step_trace": _step_trace(
                    chunk_index=chunk_index,
                    vectors=[_vector()] * 2 + [_vector(first=False)] + [_vector()] * 5,
                ),
            },
        ),
        observe_goal_predicates=lambda: deepcopy(final_vector),
        observed_reset_count=lambda: 1,
    )

    assert result["status"] == "stopped_on_preservation_violation"
    assert result["final_goal_predicate_observations"] == final_vector
    assert result["chunks_executed"] == 1
    assert result["first_preservation_violation"]["action_step_number"] == 3
    assert result["chunk_evidence"][0]["preservation_violation_predicate_ids"] == [
        proposal["preserve_predicate_ids"][0]
    ]


def test_model_instruction_mismatch_fails_closed_and_blocks_replay(tmp_path) -> None:
    proposal, approval, dispatch = _authorization()
    ledger = DispatchAuthorityTable(tmp_path / "dispatch.json")

    with pytest.raises(RuntimeError, match="instruction_binding_mismatch"):
        run_same_world_repair(
            proposal=proposal,
            approval=approval,
            dispatch=dispatch,
            dispatch_ledger=ledger,
            initial_observation={"version": 0},
            invoke_model=lambda observation, instruction, chunk_index: (
                {"chunk": 0},
                {
                    "model_runtime_invoked": True,
                    "repair_instruction_sha256": "wrong",
                },
            ),
            apply_action_chunk=lambda action, chunk_index: pytest.fail(
                "action must not reach the simulator"
            ),
            observe_goal_predicates=lambda: _vector(),
            observed_reset_count=lambda: 1,
        )

    replay = ledger.claim_dispatch_ref(
        dispatch_ref=dispatch["dispatch_ref"],
        request_payload={"different": "payload"},
    )
    assert replay["send_permitted"] is False
    assert replay["idempotency_status"] == "dispatch_ref_payload_mismatch"


def test_official_aggregate_mismatch_fails_closed(tmp_path) -> None:
    proposal, approval, dispatch = _authorization()

    with pytest.raises(RuntimeError, match="conjunction_mismatch"):
        run_same_world_repair(
            proposal=proposal,
            approval=approval,
            dispatch=dispatch,
            dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch.json"),
            initial_observation={"version": 0},
            invoke_model=lambda observation, instruction, chunk_index: (
                {"chunk": 0},
                {
                    **_invocation(proposal),
                },
            ),
            apply_action_chunk=lambda action, chunk_index: (
                {"version": 1},
                {
                    "simulator_step_return_observed": True,
                    "simulator_effect_observed": True,
                    "official_predicate_result": True,
                    "preservation_step_trace": _step_trace(
                        chunk_index=chunk_index,
                        vectors=[_vector()],
                    ),
                },
            ),
            observe_goal_predicates=lambda: _vector(),
            observed_reset_count=lambda: 1,
        )


def test_missing_step_trace_fails_closed(tmp_path) -> None:
    proposal, approval, dispatch = _authorization()

    with pytest.raises(RuntimeError, match="step_trace_not_sequence"):
        run_same_world_repair(
            proposal=proposal,
            approval=approval,
            dispatch=dispatch,
            dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch.json"),
            initial_observation={"version": 0},
            invoke_model=lambda observation, instruction, chunk_index: (
                {"chunk": 0},
                _invocation(proposal),
            ),
            apply_action_chunk=lambda action, chunk_index: (
                {"version": 1},
                {
                    "simulator_step_return_observed": True,
                    "simulator_effect_observed": True,
                    "official_predicate_result": False,
                },
            ),
            observe_goal_predicates=lambda: _vector(),
            observed_reset_count=lambda: 1,
        )


def test_step_trace_conjunction_mismatch_fails_closed(tmp_path) -> None:
    proposal, approval, dispatch = _authorization()
    trace = _step_trace(chunk_index=0, vectors=[_vector()])
    trace[4]["official_predicate_result"] = True

    with pytest.raises(RuntimeError, match="step_trace_conjunction_mismatch"):
        run_same_world_repair(
            proposal=proposal,
            approval=approval,
            dispatch=dispatch,
            dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch.json"),
            initial_observation={"version": 0},
            invoke_model=lambda observation, instruction, chunk_index: (
                {"chunk": 0},
                _invocation(proposal),
            ),
            apply_action_chunk=lambda action, chunk_index: (
                {"version": 1},
                {
                    "simulator_step_return_observed": True,
                    "simulator_effect_observed": True,
                    "official_predicate_result": False,
                    "preservation_step_trace": trace,
                },
            ),
            observe_goal_predicates=lambda: _vector(),
            observed_reset_count=lambda: 1,
        )


def test_step_trace_region_margin_mismatch_fails_closed() -> None:
    trace = _step_trace(chunk_index=0, vectors=[_vector()])
    trace[0]["object_witnesses"]["moka_pot_1"]["stove_region_witness"]["axis_margins_metres"][
        "x"
    ] += 0.01

    with pytest.raises(RuntimeError, match="region_margin_mismatch"):
        normalize_preservation_step_trace(
            environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
            chunk_index=0,
            n_action_steps=8,
            trace=trace,
        )


def _run_with_frames(tmp_path, frames: bool | str) -> dict:
    """Run one preservation-violation Repair that differs only in frame records."""

    proposal, approval, dispatch = _authorization()
    current_vector = _vector()

    def apply_action_chunk(action, chunk_index):
        current_vector[:] = _vector(first=False)
        return {"version": 1}, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": True,
            "action_chunk_sha256": "action:0",
            "official_predicate_result": False,
            "preservation_step_trace": _step_trace(
                chunk_index=chunk_index,
                vectors=[_vector()] * 3 + [_vector(first=False)] * 5,
                frames=frames,
            ),
        }

    return run_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch.json"),
        initial_observation={"version": 0},
        invoke_model=lambda observation, instruction, chunk_index: (
            {"chunk": chunk_index},
            _invocation(proposal),
        ),
        apply_action_chunk=apply_action_chunk,
        observe_goal_predicates=lambda: deepcopy(current_vector),
        observed_reset_count=lambda: 1,
    )


def test_receipt_digest_is_invariant_to_captured_frames(tmp_path, monkeypatch) -> None:
    """Frames are diagnostic, so they must not move the Repair receipt digest."""

    monkeypatch.setattr(same_world_repair, "_now", lambda: "2026-08-12T00:03:00+00:00")
    without = _run_with_frames(tmp_path / "without", frames=False)
    captured = _run_with_frames(tmp_path / "captured", frames=True)
    alternate = _run_with_frames(tmp_path / "alternate", frames="alternate")
    failed = _run_with_frames(tmp_path / "failed", frames="failed")

    digests = {run["result_sha256"] for run in (without, captured, alternate, failed)}
    assert len(digests) == 1

    for run in (without, captured, alternate, failed):
        assert run["status"] == "stopped_on_preservation_violation"
        assert run["frame_capture_authority"] == FRAME_CAPTURE_AUTHORITY
    assert without["first_preservation_violation"] == captured["first_preservation_violation"]


def test_frame_digest_separates_distinct_frames(tmp_path) -> None:
    """Frames stay tamper-evident under their own digest, outside the receipt."""

    captured = _run_with_frames(tmp_path / "captured", frames=True)
    alternate = _run_with_frames(tmp_path / "alternate", frames="alternate")
    failed = _run_with_frames(tmp_path / "failed", frames="failed")

    assert len({run["frame_capture_sha256"] for run in (captured, alternate, failed)}) == 3


def test_frame_capture_failure_does_not_stop_the_run(tmp_path) -> None:
    """A lost render must not end a run the predicates can still decide."""

    result = _run_with_frames(tmp_path, frames="failed")

    assert result["status"] == "stopped_on_preservation_violation"
    assert result["chunks_executed"] == 1
    record = result["chunk_evidence"][0]["preservation_step_trace"][0]["frame_capture"]
    assert record["status"] == "capture_failed"
    assert record["failure_code"] == "render_unavailable"
    assert record["cameras"] == []


def test_absent_frame_record_is_recorded_as_not_requested(tmp_path) -> None:
    result = _run_with_frames(tmp_path, frames=False)

    record = result["chunk_evidence"][0]["preservation_step_trace"][0]["frame_capture"]
    assert record["status"] == "not_requested"
    assert record["authority"] == FRAME_CAPTURE_AUTHORITY


def test_captured_frame_record_is_normalized() -> None:
    record = normalize_frame_capture(_frame(step=3))

    assert record["status"] == "captured"
    assert record["authority"] == FRAME_CAPTURE_AUTHORITY
    assert record["cameras"] == [
        {
            "observation_key": "video.image",
            "image_sha256": f"{3:064x}",
            "artifact_relative_path": "chunk0000/step03_image.png",
            "encoding": "png",
            "height_pixels": 256,
            "width_pixels": 256,
            "channels": 3,
        }
    ]


@pytest.mark.parametrize(
    ("mutate", "failure_code"),
    [
        (lambda record: record.update(schema_version="other"), "schema_mismatch"),
        (lambda record: record.update(status="succeeded"), "status_unknown"),
        (lambda record: record.update(cameras=[]), "cameras_empty"),
        (lambda record: record.update(cameras="frames"), "cameras_not_sequence"),
        (
            lambda record: record["cameras"][0].update(artifact_relative_path="../../etc/passwd"),
            "camera_invalid",
        ),
        (
            lambda record: record["cameras"][0].update(artifact_relative_path="/tmp/x.png"),
            "camera_invalid",
        ),
        (
            lambda record: record["cameras"][0].update(image_sha256="not-a-digest"),
            "camera_invalid",
        ),
        (
            lambda record: record["cameras"][0].update(observation_key="state.x"),
            "camera_invalid",
        ),
        (
            lambda record: record["cameras"][0].update(height_pixels=0),
            "camera_invalid",
        ),
        (
            lambda record: record["cameras"][0].update(encoding="jpeg"),
            "camera_invalid",
        ),
        (
            lambda record: record["cameras"].append(deepcopy(record["cameras"][0])),
            "camera_key_duplicated",
        ),
    ],
)
def test_malformed_frame_record_is_downgraded_not_trusted(mutate, failure_code) -> None:
    """A frame we cannot vouch for is marked unusable rather than believed."""

    record = _frame(step=0)
    mutate(record)

    normalized = normalize_frame_capture(record)

    assert normalized["status"] == "unusable"
    assert normalized["failure_code"] == failure_code
    assert normalized["cameras"] == []


def test_frame_record_never_asserts_success() -> None:
    """No frame field may carry a completion or predicate claim."""

    forbidden = {"satisfied", "official_predicate_result", "success", "task_completed"}
    record = normalize_frame_capture(_frame(step=0))

    assert not forbidden & set(record)
    assert all(not forbidden & set(camera) for camera in record["cameras"])


def _guarded_authorization(
    *,
    maximum_repair_chunks: int = 3,
    displacement_metres: float = 0.005,
    poses: dict | None = None,
):
    """Authorization whose Contract binds the preservation invariant."""

    proposal = build_same_world_repair_proposal(
        environment=LIBERO_PANDA_SCENE8_ENVIRONMENT,
        environment_session_id="libero-world:test",
        source_contract_sha256="a" * 64,
        source_goal_predicates=_vector(),
        reset_count=1,
        maximum_repair_chunks=maximum_repair_chunks,
        proposal_id="proposal:test",
        proposed_at="2026-08-12T00:00:00+00:00",
        source_object_poses=(
            {"moka_pot_1": list(MOKA_POT_1_REST_POSITION)} if poses is None else poses
        ),
        preserved_object_max_displacement_metres=displacement_metres,
    )
    approval = approve_same_world_repair(
        proposal=proposal,
        operator_approval_ref="operator:test",
        approval_id="approval:test",
        approved_at="2026-08-12T00:01:00+00:00",
    )
    dispatch = build_same_world_repair_dispatch(
        proposal=proposal,
        approval=approval,
        dispatch_ref="dispatch:test",
        created_at="2026-08-12T00:02:00+00:00",
    )
    return proposal, approval, dispatch


def _run_guarded(tmp_path, *, trace_kwargs: dict, authorization=None, vectors=None):
    proposal, approval, dispatch = authorization or _guarded_authorization()
    current_vector = _vector()
    resolved_vectors = vectors if vectors is not None else [_vector()]

    def apply_action_chunk(action, chunk_index):
        current_vector[:] = resolved_vectors[-1]
        return {"version": 1}, {
            "simulator_step_return_observed": True,
            "simulator_effect_observed": True,
            "action_chunk_sha256": "action:0",
            "official_predicate_result": False,
            "preservation_step_trace": _step_trace(
                chunk_index=chunk_index,
                vectors=resolved_vectors,
                **trace_kwargs,
            ),
        }

    return run_same_world_repair(
        proposal=proposal,
        approval=approval,
        dispatch=dispatch,
        dispatch_ledger=DispatchAuthorityTable(tmp_path / "dispatch.json"),
        initial_observation={"version": 0},
        invoke_model=lambda observation, instruction, chunk_index: (
            {"chunk": chunk_index},
            _invocation(proposal),
        ),
        apply_action_chunk=apply_action_chunk,
        observe_goal_predicates=lambda: deepcopy(current_vector),
        observed_reset_count=lambda: 1,
    )


def test_preservation_invariant_is_disabled_without_approved_reference_poses() -> None:
    """ "Keep it where it is" is meaningless without the state that was approved."""

    proposal, _, _ = _authorization()
    invariant = proposal["repair_contract"]["preservation_invariant"]

    assert invariant["enabled"] is False
    assert invariant["protected_object_names"] == []
    assert invariant["claims_completion"] is False


def test_preservation_invariant_names_objects_from_the_preserve_set() -> None:
    """Expressed over preserved predicates, not over this task's object names."""

    vector = _vector(first=True, second=False, stove=True)
    preserved = [item["predicate_id"] for item in vector if item["satisfied"]]

    names = preserved_object_names(
        goal_predicate_observations=vector,
        preserve_predicate_ids=preserved,
    )

    # on(moka_pot_1, ...) contributes its object; turnon(flat_stove_1) contributes
    # a name with no pose witness, and moka_pot_2 is a target, not preserved.
    assert names == ["moka_pot_1", "flat_stove_1"]
    assert "moka_pot_2" not in names


def test_preservation_invariant_is_bound_into_the_contract_digest() -> None:
    """A guard the operator did not approve is not a guard."""

    tight, _, _ = _guarded_authorization(displacement_metres=0.005)
    loose, _, _ = _guarded_authorization(displacement_metres=0.05)

    assert tight["repair_contract_sha256"] != loose["repair_contract_sha256"]
    assert (
        tight["repair_contract"]["preservation_invariant"]["maximum_displacement_metres"] == 0.005
    )


def test_invariant_stops_the_run_when_a_preserved_object_is_carried(tmp_path) -> None:
    result = _run_guarded(tmp_path, trace_kwargs={"lift_from_step": 2})

    assert result["status"] == "stopped_on_preservation_invariant"
    assert result["preservation_invariant_enabled"] is True
    assert result["preservation_invariant_breach_observed"] is True
    breach = result["first_preservation_invariant_breach"]
    assert breach["object_name"] == "moka_pot_1"
    assert breach["action_step_number"] == 3
    assert breach["contact_observed"] is True
    assert breach["displacement_metres"] == pytest.approx(0.011)
    assert breach["root_cause_claimed"] is False
    assert result["task_completion_claimed"] is False


def test_invariant_stops_earlier_than_the_completion_predicate_breaks(tmp_path) -> None:
    """The whole point: catch the carry before the predicate is lost."""

    result = _run_guarded(
        tmp_path,
        trace_kwargs={"lift_from_step": 2},
        vectors=[_vector()] * 6 + [_vector(first=False)] * 2,
    )

    breach = result["first_preservation_invariant_breach"]
    violation = result["first_preservation_violation"]

    assert result["status"] == "stopped_on_preservation_invariant"
    assert breach["global_repair_step_number"] == 3
    assert violation["global_repair_step_number"] == 7
    assert breach["global_repair_step_number"] < violation["global_repair_step_number"]


def test_displacement_without_contact_does_not_trip_the_invariant(tmp_path) -> None:
    """Settling physics is not the same event as an object being carried."""

    result = _run_guarded(
        tmp_path,
        trace_kwargs={"lift_from_step": 2, "lift_contact": False},
    )

    assert result["preservation_invariant_breach_observed"] is False
    assert result["status"] != "stopped_on_preservation_invariant"


def test_contact_within_the_approved_tolerance_does_not_trip(tmp_path) -> None:
    result = _run_guarded(
        tmp_path,
        trace_kwargs={"lift_from_step": 2, "lift_metres": 0.001},
    )

    assert result["preservation_invariant_breach_observed"] is False


def test_invariant_cannot_produce_a_completion_claim(tmp_path) -> None:
    """A stop-shaped guard must have no path to asserting success."""

    result = _run_guarded(tmp_path, trace_kwargs={"lift_from_step": 0})
    breach = result["first_preservation_invariant_breach"]

    assert result["status"] == "stopped_on_preservation_invariant"
    assert result["task_completion_claimed"] is False
    assert result["predicate_improvement_observed"] is False
    forbidden = {"satisfied", "official_predicate_result", "success", "task_completed"}
    assert not forbidden & set(breach)
    assert result["evidence_types_separated"] == [
        "completion_predicate",
        "preservation_invariant",
    ]


@pytest.mark.parametrize("displacement", [0.0, -0.001, float("nan")])
def test_invalid_invariant_threshold_is_rejected(displacement) -> None:
    with pytest.raises(ValueError, match="preservation_invariant_displacement"):
        _guarded_authorization(displacement_metres=displacement)


def test_malformed_reference_pose_is_rejected() -> None:
    with pytest.raises(ValueError, match="preservation_invariant_reference_pose"):
        _guarded_authorization(poses={"moka_pot_1": [0.1, 0.2]})
