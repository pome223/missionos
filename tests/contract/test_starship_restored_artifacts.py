from copy import deepcopy
from hashlib import sha256

import pytest

from src.runtime import starship_restored_artifacts as artifacts


def ref(path, raw):
    return {"path": path, "sha256": sha256(raw).hexdigest()}


@pytest.fixture
def inputs(tmp_path):
    (tmp_path/"origin.json").write_bytes(b'{"synthetic":true}')
    row = ref("origin.json", (tmp_path/"origin.json").read_bytes())
    governance = {"closed_origin": {"raw": row}, "forecast_batches": [], "current_origin_changes": []}
    return tmp_path, governance


def test_only_explicit_contract_references_are_loaded_and_rehashed(inputs):
    root, governance = inputs
    (root/"unrelated-private-file").write_text("must not be read")
    governance["free_prose"] = {"path": "unrelated-private-file", "sha256": "f"*64}
    result = artifacts.load_restored_native_artifacts(governance, root)
    assert result == {governance["closed_origin"]["raw"]["sha256"]: b'{"synthetic":true}'}


@pytest.mark.parametrize("path", ["../escape", "/escape", "./origin.json", "a/../origin.json", "a\\b", "bad\x00name"])
def test_paths_cannot_escape_or_change_representation(inputs, path):
    root, governance = inputs
    governance["closed_origin"]["raw"]["path"] = path
    with pytest.raises(ValueError, match="path_escape"):
        artifacts.load_restored_native_artifacts(governance, root)


def test_symlink_file_or_parent_is_never_read(inputs, tmp_path):
    root, governance = inputs
    (root/"linked").symlink_to(root/"origin.json")
    governance["closed_origin"]["raw"]["path"] = "linked"
    with pytest.raises(ValueError, match="symlink_forbidden"):
        artifacts.load_restored_native_artifacts(governance, root)
    (root/"linked-dir").symlink_to(root, target_is_directory=True)
    governance["closed_origin"]["raw"]["path"] = "linked-dir/origin.json"
    with pytest.raises(ValueError, match="symlink_forbidden"):
        artifacts.load_restored_native_artifacts(governance, root)


def test_byte_mutation_and_budget_are_rejected(inputs, monkeypatch):
    root, governance = inputs
    (root/"origin.json").write_bytes(b'{"synthetic":false}')
    with pytest.raises(ValueError, match="bytes_changed"):
        artifacts.load_restored_native_artifacts(governance, root)
    monkeypatch.setattr(artifacts, "MAX_FILE_BYTES", 1)
    with pytest.raises(ValueError, match="byte_budget"):
        artifacts.load_restored_native_artifacts(governance, root)


def test_same_path_cannot_be_rebound_in_a_later_origin(inputs):
    root, governance = inputs
    changed = deepcopy(governance["closed_origin"]["raw"])
    changed["sha256"] = "a"*64
    governance["current_origin_changes"] = [{"origin": changed}]
    with pytest.raises(ValueError, match="path_rebound"):
        artifacts.load_restored_native_artifacts(governance, root)
