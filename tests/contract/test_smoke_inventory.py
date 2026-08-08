from pathlib import Path
import importlib.util


REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER_PATH = REPO_ROOT / "scripts" / "check_smoke_inventory.py"


def _checker_module():
    spec = importlib.util.spec_from_file_location("check_smoke_inventory", CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_python_smoke_has_exactly_one_disposition() -> None:
    result = _checker_module().verify_inventory()

    assert result["ok"] is True
    assert result["actual_count"] == 95
    assert result["classified_count"] == 95
    assert result["duplicates"] == []
    assert result["missing"] == []
    assert result["absent"] == []


def test_inventory_keeps_live_and_authority_boundaries_out_of_deletion_queue() -> None:
    result = _checker_module().verify_inventory()
    actions = result["category_actions"]

    assert actions["production_runtime_harness"] == "retain_then_rename_or_move"
    assert actions["ci_runtime_contract"] == "retain_as_canonical_ci_boundary"
    assert actions["opt_in_live_integration"] == "retain_opt_in_and_never_run_by_default"
    assert actions["artifact_contract_candidate"] == (
        "migrate_to_shared_pytest_before_removal"
    )
