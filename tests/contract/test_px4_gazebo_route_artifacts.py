from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import artifacts


FIXED_TIME = datetime(2026, 7, 15, 12, 34, 56, tzinfo=timezone.utc)


def test_legacy_entrypoint_delegates_artifact_io_to_package() -> None:
    assert route_entrypoint._new_run_dir is artifacts.create_run_directory
    assert route_entrypoint._write_json is artifacts.write_json
    assert route_entrypoint._write_jsonl is artifacts.write_jsonl
    assert route_entrypoint._mark_cleanup_observed is artifacts.mark_cleanup_observed


def test_artifact_root_reads_only_explicit_environment_mapping(tmp_path: Path) -> None:
    assert artifacts.artifact_root(environ={}) == Path(artifacts.DEFAULT_ARTIFACT_ROOT)
    assert artifacts.artifact_root(environ={artifacts.ARTIFACT_ROOT_ENV: str(tmp_path)}) == tmp_path


def test_run_directory_uses_timestamp_and_collision_suffix(tmp_path: Path) -> None:
    first = artifacts.create_run_directory(root=tmp_path, observed_at=FIXED_TIME)
    second = artifacts.create_run_directory(root=tmp_path, observed_at=FIXED_TIME)

    assert first.name == "horizontal_route_20260715T123456Z"
    assert second.name == "horizontal_route_20260715T123456Z_2"
    assert first.is_dir()
    assert second.is_dir()


def test_json_and_jsonl_serialization_are_stable(tmp_path: Path) -> None:
    json_path = tmp_path / "summary.json"
    jsonl_path = tmp_path / "samples.jsonl"

    artifacts.write_json(json_path, {"z": 2, "a": 1})
    artifacts.write_jsonl(jsonl_path, [{"z": 2}, {"a": 1}])

    assert json_path.read_text() == '{\n  "a": 1,\n  "z": 2\n}\n'
    assert jsonl_path.read_text() == '{"z": 2}\n{"a": 1}\n'


def test_cleanup_receipt_update_preserves_claim_boundaries(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    artifacts.write_json(
        summary_path,
        {
            "scenario_cleanup_receipt": {"cleanup_status": "teardown_required"},
            "dispatch_authority_created": False,
            "physical_execution_invoked": False,
            "delivery_completion_claimed": False,
        },
    )

    assert artifacts.mark_cleanup_observed(tmp_path, observed_at=FIXED_TIME) is True
    summary = json.loads(summary_path.read_text())
    assert (
        summary["scenario_cleanup_receipt"]["cleanup_status"]
        == "isolated_container_teardown_observed"
    )
    assert summary["scenario_cleanup_receipt"]["observed_at"] == FIXED_TIME.isoformat()
    assert summary["dispatch_authority_created"] is False
    assert summary["physical_execution_invoked"] is False
    assert summary["delivery_completion_claimed"] is False


def test_cleanup_receipt_update_is_noop_without_receipt(tmp_path: Path) -> None:
    assert artifacts.mark_cleanup_observed(tmp_path, observed_at=FIXED_TIME) is False
    artifacts.write_json(tmp_path / "summary.json", {"final_status": "blocked"})
    assert artifacts.mark_cleanup_observed(tmp_path, observed_at=FIXED_TIME) is False
