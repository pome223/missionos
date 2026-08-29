from __future__ import annotations

import numpy as np
import json

from scripts import probe_libero_book_caddy_fixture as fixture
from scripts import run_cosmos_policy_libero_book_caddy_probe as cosmos_probe
from scripts import run_libero_book_caddy_oracle as oracle
from scripts import run_vla0_libero_book_caddy_probe as vla0_probe


def test_book_caddy_profile_matches_official_libero10_index() -> None:
    assert fixture.TASK_ID == 5
    assert fixture.TARGET_OBJECT == "black_book_1"
    assert fixture.CONTAINER_OBJECT == "desk_caddy_1"
    assert fixture.TASK_INSTRUCTION == (
        "pick up the book and place it in the back compartment of the caddy"
    )


def test_visibility_counts_pixels_not_changed_channels() -> None:
    success = {
        "agentview": np.zeros((16, 16, 3), dtype=np.uint8),
        "wrist": np.zeros((16, 16, 3), dtype=np.uint8),
    }
    changed = {name: image.copy() for name, image in success.items()}
    changed["agentview"][0, 0] = [1, 2, 3]
    changed["wrist"][:, :] = [2, 2, 2]

    result = fixture._visibility(success, changed)

    assert result["agentview"]["changed_pixel_count"] == 1
    assert result["wrist"]["changed_pixel_count"] == 256
    assert result["wrist"]["changed_pixel_fraction"] == 1.0


def test_fixture_digest_is_key_order_independent() -> None:
    assert fixture.canonical_sha256({"a": 1, "b": 2}) == fixture.canonical_sha256(
        {"b": 2, "a": 1}
    )


def test_oracle_admits_only_digest_bound_book_caddy_fixture(tmp_path) -> None:
    fixture_material = {
        "schema_version": fixture.FIXTURE_SCHEMA_VERSION,
        "fixture_admitted": True,
        "terminal_goal_predicate_vector": [False],
    }
    state = np.asarray([1.0, 2.0, 3.0], dtype=np.float64)
    metadata = {
        "simulator_state_sha256": __import__("hashlib").sha256(state.tobytes()).hexdigest(),
        "task_suite": fixture.TASK_SUITE,
        "task_id": fixture.TASK_ID,
        "task_name": fixture.TASK_NAME,
        "episode_init_state_index": fixture.EPISODE_INIT_STATE_INDEX,
        "environment_seed": fixture.ENVIRONMENT_SEED,
        "source_failure_basis": "diagnostic_book_caddy_displacement",
        "source_goal_predicate_vector": [False],
        "source_failure_is_repair_candidate": True,
        "book_caddy_fixture": fixture_material,
        "book_caddy_fixture_sha256": fixture.canonical_sha256(fixture_material),
    }
    path = tmp_path / "fixture.npz"
    np.savez_compressed(
        path,
        simulator_state=state,
        metadata_json=np.asarray(json.dumps(metadata, sort_keys=True)),
    )

    observed_state, observed_metadata = oracle._read_snapshot(path)

    assert np.array_equal(observed_state, state)
    assert observed_metadata["book_caddy_fixture"] == fixture_material


def test_cosmos_probe_requires_stable_digest_bound_positive_control(tmp_path) -> None:
    snapshot = tmp_path / "fixture.npz"
    snapshot.write_bytes(b"fixture")
    report_without_digest = {
        "snapshot_sha256": cosmos_probe._sha256_path(snapshot),
        "task_id": fixture.TASK_ID,
        "task_name": fixture.TASK_NAME,
        "stable_success_observed": True,
        "stable_success_steps_completed": cosmos_probe.STABLE_SUCCESS_STEPS,
        "terminal_goal_predicate_vector": [True],
        "preservation_violation_observed": False,
        "actions_applied": 158,
        "first_contact_after_action": 9,
        "success_first_observed_after_action": 13,
        "claim_boundary": {
            "model_inference_invoked": False,
            "physical_execution_invoked": False,
        },
    }
    report = {
        **report_without_digest,
        "result_sha256": fixture.canonical_sha256(report_without_digest),
    }
    path = tmp_path / "oracle.json"
    path.write_text(json.dumps(report))

    admission = cosmos_probe._verify_oracle(path, snapshot)

    assert admission["first_contact_after_action"] == 9
    assert admission["stable_success_steps_completed"] == 20


def test_vla0_probe_requires_the_same_positive_control_contract(tmp_path) -> None:
    snapshot = tmp_path / "fixture.npz"
    snapshot.write_bytes(b"fixture")
    report_without_digest = {
        "snapshot_sha256": vla0_probe._sha256_path(snapshot),
        "task_id": fixture.TASK_ID,
        "stable_success_observed": True,
        "stable_success_steps_completed": vla0_probe.STABLE_SUCCESS_STEPS,
        "terminal_goal_predicate_vector": [True],
        "preservation_violation_observed": False,
        "actions_applied": 33,
        "first_contact_after_action": 9,
        "success_first_observed_after_action": 13,
    }
    report = {
        **report_without_digest,
        "result_sha256": fixture.canonical_sha256(report_without_digest),
    }
    path = tmp_path / "oracle.json"
    path.write_text(json.dumps(report))

    admission = vla0_probe._verify_oracle(path, snapshot)

    assert admission["actions_applied"] == 33
    assert admission["stable_success_steps_completed"] == 20
