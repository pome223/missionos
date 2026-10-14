"""Lossless intermediate storage; no models or simulated flight."""
from hashlib import sha256
import gzip
import json
from pathlib import Path

import pytest

from src.runtime import starship_artifacts as artifacts


def fixture_value():
    return {"source_sha256": {"physics.py": "a" * 64}, "negative_outcome": True,
            "samples": [(i, [1.0, 2.0, 3.0]) for i in range(100)],
            "schema": "fixture.saved_study.v1"}


def test_lossless_checkpoint_keeps_raw_bytes_and_ordinary_outer_study(tmp_path):
    value = fixture_value()
    expected_raw = (json.dumps(value, indent=2, allow_nan=False) + "\n").encode()
    checkpoint = tmp_path / "flight-study.json"
    result = artifacts.write_verified_checkpoint(checkpoint, value)
    reference = json.loads(checkpoint.read_bytes())
    payload = tmp_path / reference["artifact"]
    assert reference["schema"] == artifacts.CHECKPOINT_SCHEMA
    assert reference["json_sha256"] == sha256(expected_raw).hexdigest()
    assert gzip.decompress(payload.read_bytes()) == expected_raw
    assert reference["stored_sha256"] == sha256(payload.read_bytes()).hexdigest()
    assert reference["json_bytes"] == len(expected_raw)
    assert reference["stored_bytes"] + checkpoint.stat().st_size < len(expected_raw)
    assert result == json.loads(expected_raw) == artifacts.read_verified_input(checkpoint)
    assert not list(tmp_path.glob("*.pending"))
    outer = artifacts.write_verified_input(tmp_path / "study.json", {"inner": result})
    assert json.loads((tmp_path / "study.json").read_bytes()) == outer
    assert artifacts.read_verified_input(tmp_path / "study.json") == outer


def test_publication_waits_for_durable_payload_and_never_replaces_evidence(tmp_path, monkeypatch):
    path = tmp_path / "study.json"
    original_link = artifacts.os.link
    def publish(source, target, **kwargs):
        assert target == path and not target.exists()
        reference = json.loads(Path(source).read_bytes())
        raw = gzip.decompress((tmp_path / reference["artifact"]).read_bytes())
        assert reference["json_sha256"] == sha256(raw).hexdigest()
        original_link(source, target, **kwargs)
    monkeypatch.setattr(artifacts.os, "link", publish)
    artifacts.write_verified_checkpoint(path, fixture_value())
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(FileExistsError):
        artifacts.write_verified_checkpoint(path, {"replacement": True})
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_interrupted_payload_write_never_publishes_completion_marker(tmp_path, monkeypatch):
    def failed_sync(_):
        raise OSError("injected durable-write failure")
    monkeypatch.setattr(artifacts.os, "fsync", failed_sync)
    with pytest.raises(OSError):
        artifacts.write_verified_checkpoint(tmp_path / "flight-study.json", fixture_value())
    assert not (tmp_path / "flight-study.json").exists()
    assert (tmp_path / "flight-study.json.gz").exists()  # retained, counted partial evidence


@pytest.mark.parametrize("change", ["stored_hash", "stored_length", "json_hash", "json_length", "cap", "path", "extra"])
def test_modified_reference_is_rejected(tmp_path, change):
    path = tmp_path / "study.json"
    artifacts.write_verified_checkpoint(path, fixture_value())
    reference = json.loads(path.read_bytes())
    if change == "stored_hash": reference["stored_sha256"] = "0" * 64
    elif change == "stored_length": reference["stored_bytes"] += 1
    elif change == "json_hash": reference["json_sha256"] = "0" * 64
    elif change == "json_length": reference["json_bytes"] -= 1
    elif change == "cap": reference["json_bytes"] = artifacts.MAX_CHECKPOINT_JSON_BYTES + 1
    elif change == "path": reference["artifact"] = "../outside.json.gz"
    else: reference["unexpected"] = True
    path.write_text(json.dumps(reference))
    with pytest.raises(ValueError, match="checkpoint_"):
        artifacts.read_verified_input(path)


def test_corrupt_compressed_bytes_and_symlink_are_rejected(tmp_path):
    path = tmp_path / "study.json"
    artifacts.write_verified_checkpoint(path, fixture_value())
    payload = tmp_path / "study.json.gz"
    original = payload.read_bytes()
    payload.write_bytes(original[:-1])
    with pytest.raises(ValueError, match="checkpoint_"):
        artifacts.read_verified_input(path)
    payload.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(original)
    payload.symlink_to(outside)
    with pytest.raises((OSError, ValueError)):
        artifacts.read_verified_input(path)


def test_decode_is_bounded_by_declared_length(tmp_path):
    path = tmp_path / "study.json"
    artifacts.write_verified_checkpoint(path, {"payload": "x" * 100_000})
    reference = json.loads(path.read_bytes())
    reference["json_bytes"] = 20
    path.write_text(json.dumps(reference))
    with pytest.raises(ValueError, match="checkpoint_json_hash_or_length"):
        artifacts.read_verified_input(path)


def test_invalid_json_or_size_does_not_leave_a_checkpoint(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        artifacts.write_verified_checkpoint(tmp_path / "invalid.json", {"x": float("nan")})
    monkeypatch.setattr(artifacts, "MAX_CHECKPOINT_JSON_BYTES", 10)
    with pytest.raises(ValueError, match="checkpoint_json_size"):
        artifacts.write_verified_checkpoint(tmp_path / "large.json", fixture_value())
    assert not list(tmp_path.iterdir())
