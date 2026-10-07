"""Filesystem resource fixtures; no model calls or physical integration."""
import gzip
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from src.runtime import starship_static_artifacts as artifacts
from src.runtime.starship_actual_recovery_shooting import _persist


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def free_space(monkeypatch, free=2*1024**3):
    value = SimpleNamespace(free=free)
    monkeypatch.setattr(artifacts.shutil, "disk_usage", lambda _: value)
    return value


def incompressible_data(count=4000):
    return "".join(sha256(str(i).encode()).hexdigest() for i in range(count))


def test_manifest_gzip_hashes_and_exact_durable_byte_accounting(tmp_path, monkeypatch):
    free_space(monkeypatch)
    root = tmp_path/"corpus"
    sink = artifacts.BoundedStaticArtifactSink(root)
    payload = {"kind": "static_model_attempted", "hypothetical_not_integrated": True}
    sink.check_before_query()
    manifest = _persist(sink, payload)
    saved = (root/manifest["relative_path"]).read_bytes()
    assert gzip.decompress(saved) == canonical(payload)
    assert sha256(saved).hexdigest() == manifest["sha256"]
    assert sha256(canonical(payload)).hexdigest() == manifest["raw_json_sha256"]
    assert manifest["format"] == "json.gz" and manifest["persisted_before_analysis"] is True
    status = sink.resource_status()
    assert status["persisted_bytes"] == sum(file.stat().st_size for file in root.iterdir())
    assert status["persisted_bytes"] == status["artifact_bytes"]+status["ledger_bytes"]
    ledger = [json.loads(line) for line in (root/artifacts.LEDGER_NAME).read_text().splitlines()]
    assert ledger == sink.resource_ledger()
    assert ledger[1]["kind"] == "before_query_resource_check"
    assert ledger[-1]["manifest"] == manifest
    inventory = sink.artifact_inventory()
    assert inventory["io_incomplete"] is False
    assert inventory["missing_resource_ledger"] is False
    assert inventory["bytes_from_regular_files"] == status["persisted_bytes"]
    sink.close()
    assert all(file.exists() for file in root.iterdir())


def test_stored_cap_stops_without_retry_but_preserves_reserved_summary(tmp_path, monkeypatch):
    free_space(monkeypatch)
    root = tmp_path/"corpus"
    sink = artifacts.BoundedStaticArtifactSink(root, maximum_stored_bytes=8192, summary_reserve_bytes=4096)
    first = _persist(sink, {"kind": "static_model_attempted", "query": 1})
    saved = (root/first["relative_path"]).read_bytes()
    with pytest.raises(artifacts.StaticArtifactLimit, match="stored_byte_cap"):
        _persist(sink, {"kind": "static_model_raw", "large_fixture": incompressible_data(400)})
    assert sink.resource_status()["model_query_allowed"] is False
    assert sink.resource_status()["stop_reason"] == "static_artifact_stored_byte_cap"
    with pytest.raises(artifacts.StaticArtifactLimit, match="stopped_without_retry"):
        _persist(sink, {"kind": "static_model_attempted", "query": 2})
    final = _persist(sink, {"kind": artifacts.FINAL_KIND, "failure": "stored_byte_cap", "retained_attempt": first})
    assert (root/first["relative_path"]).read_bytes() == saved
    assert (root/final["relative_path"]).is_file()
    assert sink.resource_status()["final_summary_written"] is True
    assert sink.persisted_bytes <= 8192
    with pytest.raises(artifacts.StaticArtifactLimit, match="closed"):
        _persist(sink, {"kind": artifacts.FINAL_KIND})
    sink.close()


def test_free_guard_is_checked_again_before_every_query_batch(tmp_path, monkeypatch):
    free = free_space(monkeypatch)
    sink = artifacts.BoundedStaticArtifactSink(tmp_path/"corpus")
    sink.check_before_query()
    first = _persist(sink, {"kind": "static_model_attempted"})
    free.free = artifacts.MINIMUM_FREE_BYTES-1
    assert sink.resource_status()["model_query_allowed"] is False
    with pytest.raises(artifacts.StaticArtifactLimit, match="resources_unavailable"):
        sink.check_before_query()
    with pytest.raises(artifacts.StaticArtifactLimit, match="free_space_guard"):
        _persist(sink, {"kind": "static_model_raw"})
    assert (tmp_path/"corpus"/first["relative_path"]).is_file()
    sink.close()


def test_projected_post_write_free_space_must_retain_guard(tmp_path, monkeypatch):
    free = free_space(monkeypatch)
    sink = artifacts.BoundedStaticArtifactSink(tmp_path/"corpus")
    free.free = artifacts.MINIMUM_FREE_BYTES+1
    with pytest.raises(artifacts.StaticArtifactLimit, match="free_space_guard"):
        _persist(sink, {"kind": "static_model_attempted"})
    assert list((tmp_path/"corpus").iterdir()) == [tmp_path/"corpus"/artifacts.LEDGER_NAME]
    sink.close()


@pytest.mark.parametrize("kwargs", [
    {"maximum_stored_bytes": artifacts.MAXIMUM_STORED_BYTES+1},
    {"minimum_free_bytes": artifacts.MINIMUM_FREE_BYTES-1},
    {"maximum_stored_bytes": True}, {"summary_reserve_bytes": 0},
    {"summary_reserve_bytes": artifacts.MAXIMUM_STORED_BYTES},
])
def test_bounds_can_only_reduce_storage_or_increase_free_space(tmp_path, kwargs):
    with pytest.raises(artifacts.StaticArtifactLimit, match="bounds_required"):
        artifacts.BoundedStaticArtifactSink(tmp_path/"corpus", **kwargs)
    assert not (tmp_path/"corpus").exists()


def test_existing_directory_symlink_and_identity_overwrite_are_rejected(tmp_path, monkeypatch):
    free_space(monkeypatch)
    existing = tmp_path/"existing"
    existing.mkdir()
    with pytest.raises(artifacts.StaticArtifactLimit, match="fresh"):
        artifacts.BoundedStaticArtifactSink(existing)
    link = tmp_path/"link"
    link.symlink_to(existing, target_is_directory=True)
    with pytest.raises(artifacts.StaticArtifactLimit, match="fresh"):
        artifacts.BoundedStaticArtifactSink(link)
    sink = artifacts.BoundedStaticArtifactSink(tmp_path/"corpus")
    identity = "a"*32
    first = sink(identity, {"kind": "static_model_attempted"})
    raw = (tmp_path/"corpus"/first["relative_path"]).read_bytes()
    with pytest.raises(artifacts.StaticArtifactLimit, match="already_written"):
        sink(identity, {"kind": "static_model_raw"})
    assert (tmp_path/"corpus"/first["relative_path"]).read_bytes() == raw
    sink.close()


def test_partial_write_failure_retains_bytes_and_does_not_retry(tmp_path, monkeypatch):
    free_space(monkeypatch)
    sink = artifacts.BoundedStaticArtifactSink(tmp_path/"corpus")
    original_write = artifacts.os.write
    calls = 0
    def short_then_failure(descriptor, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_write(descriptor, data[:10])
        raise OSError("fixture write failure")
    monkeypatch.setattr(artifacts.os, "write", short_then_failure)
    identity = "a"*32
    with pytest.raises(OSError, match="fixture"):
        sink(identity, {"kind": "static_model_raw", "test": 1})
    retained = tmp_path/"corpus"/(identity+".json.gz")
    assert retained.exists() and retained.stat().st_size == 10
    assert sink.persisted_bytes == sum(file.stat().st_size for file in (tmp_path/"corpus").iterdir())
    assert sink.resource_status()["stop_reason"] == "OSError"
    assert sink.artifact_inventory()["orphaned_or_partial_artifact_names"] == [retained.name]
    monkeypatch.setattr(artifacts.os, "write", original_write)
    with pytest.raises(artifacts.StaticArtifactLimit, match="stopped_without_retry"):
        sink("b"*32, {"kind": "static_model_raw"})
    final = sink("c"*32, {"kind": artifacts.FINAL_KIND, "incomplete_artifact": identity})
    assert retained.stat().st_size == 10 and (tmp_path/"corpus"/final["relative_path"]).exists()
    sink.close()


def test_closed_sink_does_not_write_anything_else(tmp_path, monkeypatch):
    free_space(monkeypatch)
    sink = artifacts.BoundedStaticArtifactSink(tmp_path/"corpus")
    before = sink.persisted_bytes
    sink.close()
    assert sink.resource_status()["model_query_allowed"] is False
    with pytest.raises(artifacts.StaticArtifactLimit, match="closed"):
        sink("a"*32, {"kind": "static_model_raw"})
    assert sink.persisted_bytes == before


@pytest.mark.parametrize("failure", ["free-space", "write-fault"])
def test_failed_initialization_closes_handles_and_preserves_ledger(tmp_path, monkeypatch, failure):
    free_space(monkeypatch, artifacts.MINIMUM_FREE_BYTES-1 if failure == "free-space" else 2*1024**3)
    actual_close = artifacts.os.close
    closed = []
    def close(descriptor):
        closed.append(descriptor)
        actual_close(descriptor)
    monkeypatch.setattr(artifacts.os, "close", close)
    if failure == "write-fault":
        monkeypatch.setattr(artifacts.os, "write", lambda *a: (_ for _ in ()).throw(OSError("fixture init fault")))
    with pytest.raises((artifacts.StaticArtifactLimit, OSError)):
        artifacts.BoundedStaticArtifactSink(tmp_path/"corpus")
    assert len(closed) == 2
    assert (tmp_path/"corpus"/artifacts.LEDGER_NAME).is_file()


def test_ledger_failure_preserves_orphan_raw_and_marks_incomplete(tmp_path, monkeypatch):
    free_space(monkeypatch)
    sink = artifacts.BoundedStaticArtifactSink(tmp_path/"corpus")
    original_write = artifacts.os.write
    def fail_ledger(descriptor, data):
        if descriptor == sink._ledger_fd:
            raise OSError("fixture ledger write failed")
        return original_write(descriptor, data)
    monkeypatch.setattr(artifacts.os, "write", fail_ledger)
    identity = "a"*32
    with pytest.raises(OSError, match="ledger write"):
        sink(identity, {"kind": "static_model_raw", "retained": True})
    orphan = tmp_path/"corpus"/(identity+".json.gz")
    assert json.loads(gzip.decompress(orphan.read_bytes()))["retained"] is True
    inventory = sink.artifact_inventory()
    assert inventory["io_incomplete"] is True and inventory["ledger_write_incomplete"] is True
    assert inventory["orphaned_or_partial_artifact_names"] == [orphan.name]
    assert inventory["ledger_coverage_is_in_memory_assertion"] is True
    assert inventory["filesystem_durability_authenticated"] is False
    assert inventory["bytes_from_regular_files"] == sink.persisted_bytes
    monkeypatch.setattr(artifacts.os, "write", original_write)
    with pytest.raises(artifacts.StaticArtifactLimit, match="stopped_without_retry"):
        sink("b"*32, {"kind": "static_model_attempted"})
    sink.close()
    assert orphan.is_file()


def test_inventory_flags_unexpected_names_and_rejects_symlinks(tmp_path, monkeypatch):
    free_space(monkeypatch)
    sink = artifacts.BoundedStaticArtifactSink(tmp_path/"corpus")
    extra = tmp_path/"corpus"/"unexpected.txt"
    extra.write_text("public fixture")
    entry = next(row for row in sink.artifact_inventory()["entries"] if row["name"] == extra.name)
    assert entry["expected_name"] is False and "sha256" not in entry
    assert sink.artifact_inventory()["io_incomplete"] is True
    (tmp_path/"corpus"/"link").symlink_to(extra)
    with pytest.raises(artifacts.StaticArtifactLimit, match="nonregular"):
        sink.artifact_inventory()
    sink.close()


def test_actual_trial_raw_has_same_reserved_terminal_storage_and_closes_sink(tmp_path, monkeypatch):
    free_space(monkeypatch)
    root = tmp_path/"corpus"
    sink = artifacts.BoundedStaticArtifactSink(root, maximum_stored_bytes=8192, summary_reserve_bytes=4096)
    _persist(sink, {"kind": "coupled_pin_step_receipt", "step_index": 0})
    with pytest.raises(artifacts.StaticArtifactLimit, match="stored_byte_cap"):
        _persist(sink, {"kind": "coupled_pin_step_receipt", "large_public_fixture": incompressible_data(400)})
    result = {"scenario": "booster_coupled_pin_trial", "outcome": {"termination": "fixture_resource_stop"}}
    final = _persist(sink, {"kind": artifacts.ACTUAL_TRIAL_FINAL_KIND, "result": result})
    assert json.loads(gzip.decompress((root/final["relative_path"]).read_bytes()))["result"] == result
    assert sink.resource_status()["final_summary_written"] is True
    assert sink.persisted_bytes <= 8192
    with pytest.raises(artifacts.StaticArtifactLimit, match="closed"):
        _persist(sink, {"kind": "coupled_pin_step_receipt", "step_index": 1})
    sink.close()


class FixtureHardDeadline(BaseException):
    pass


@pytest.mark.parametrize("target,full_write", [("artifact", False), ("artifact", True), ("ledger", False)])
def test_baseexception_after_kernel_write_accounts_and_retains_incomplete_evidence(
        tmp_path, monkeypatch, target, full_write):
    free_space(monkeypatch)
    root = tmp_path/"corpus"
    sink = artifacts.BoundedStaticArtifactSink(root)
    original_write = artifacts.os.write
    original_fsync = artifacts.os.fsync
    synced = []

    def interrupted_write(descriptor, data):
        is_target = (descriptor == sink._ledger_fd) == (target == "ledger")
        if is_target:
            original_write(descriptor, data if full_write else data[:7])
            raise FixtureHardDeadline()
        return original_write(descriptor, data)

    def fsync(descriptor):
        synced.append(descriptor)
        return original_fsync(descriptor)

    monkeypatch.setattr(artifacts.os, "write", interrupted_write)
    monkeypatch.setattr(artifacts.os, "fsync", fsync)
    identity = "a"*32
    with pytest.raises(FixtureHardDeadline):
        sink(identity, {"kind": "coupled_pin_step_receipt", "pre_step_state": [1, 2, 3]})
    retained = root/(identity+".json.gz")
    inventory = sink.artifact_inventory()
    status = sink.resource_status()
    assert status["persisted_bytes"] == sum(path.stat().st_size for path in root.iterdir())
    assert status["persisted_bytes"] == status["artifact_bytes"]+status["ledger_bytes"]
    assert status["stop_reason"] == "FixtureHardDeadline" and status["model_query_allowed"] is False
    assert inventory["io_incomplete"] is True
    assert inventory["ledger_write_incomplete"] is (target == "ledger")
    assert inventory["orphaned_or_partial_artifact_names"] == [retained.name]
    assert synced and sink._directory_fd in synced
    raw_before = retained.read_bytes()
    ledger_before = (root/artifacts.LEDGER_NAME).read_bytes()
    monkeypatch.setattr(artifacts.os, "write", original_write)
    with pytest.raises(artifacts.StaticArtifactLimit, match="stopped_without_retry"):
        sink("b"*32, {"kind": "coupled_pin_step_receipt"})
    footer = sink("c"*32, {"kind": artifacts.ACTUAL_TRIAL_FINAL_KIND,
        "failure": "FixtureHardDeadline", "last_committed_state_unknown": True})
    assert retained.read_bytes() == raw_before
    assert (root/artifacts.LEDGER_NAME).read_bytes().startswith(ledger_before)
    assert (root/footer["relative_path"]).is_file()
    assert sink.artifact_inventory()["io_incomplete"] is True
    assert sink.persisted_bytes == sum(path.stat().st_size for path in root.iterdir())
    sink.close()


def test_interrupted_write_sync_failure_preserves_original_baseexception(tmp_path, monkeypatch):
    free_space(monkeypatch)
    root = tmp_path/"corpus"
    sink = artifacts.BoundedStaticArtifactSink(root)
    original_write = artifacts.os.write

    def interrupted(descriptor, data):
        original_write(descriptor, data[:9])
        raise FixtureHardDeadline()

    monkeypatch.setattr(artifacts.os, "write", interrupted)
    monkeypatch.setattr(artifacts.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("fixture sync fault")))
    with pytest.raises(FixtureHardDeadline):
        sink("a"*32, {"kind": "coupled_pin_step_receipt"})
    assert sink.persisted_bytes == sum(path.stat().st_size for path in root.iterdir())
    assert sink.artifact_inventory()["write_sync_incomplete"] is True
    sink.close()


def test_baseexception_initialization_closes_handles_and_preserves_written_ledger(tmp_path, monkeypatch):
    free_space(monkeypatch)
    original_write = artifacts.os.write
    original_close = artifacts.os.close
    closed = []

    def interrupted(descriptor, data):
        original_write(descriptor, data[:11])
        raise FixtureHardDeadline()

    def close(descriptor):
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(artifacts.os, "write", interrupted)
    monkeypatch.setattr(artifacts.os, "close", close)
    with pytest.raises(FixtureHardDeadline):
        artifacts.BoundedStaticArtifactSink(tmp_path/"corpus")
    assert len(closed) == 2
    assert (tmp_path/"corpus"/artifacts.LEDGER_NAME).stat().st_size == 11


@pytest.mark.parametrize("target", ["artifact", "ledger"])
def test_failed_fstat_and_reconciliation_cannot_mask_original_interrupt(tmp_path, monkeypatch, target):
    free_space(monkeypatch)
    root = tmp_path/"corpus"
    sink = artifacts.BoundedStaticArtifactSink(root)
    original_write, original_fstat = artifacts.os.write, artifacts.os.fstat
    interrupted = FixtureHardDeadline()
    after_write = False

    def write_then_interrupt(descriptor, data):
        nonlocal after_write
        if (descriptor == sink._ledger_fd) != (target == "ledger"):
            return original_write(descriptor, data)
        original_write(descriptor, data[:13])
        after_write = True
        raise interrupted

    def fstat(descriptor):
        if after_write:
            raise OSError("fixture read-only accounting failure")
        return original_fstat(descriptor)

    monkeypatch.setattr(artifacts.os, "write", write_then_interrupt)
    monkeypatch.setattr(artifacts.os, "fstat", fstat)
    with pytest.raises(FixtureHardDeadline) as caught:
        sink("a"*32, {"kind": "coupled_pin_step_receipt"})
    assert caught.value is interrupted
    status = sink.resource_status()
    assert status["reconciliation_incomplete"] is True and status["byte_accounting_known"] is False
    assert status["persisted_bytes"] is None and status["model_query_allowed"] is False
    assert status["reconciliation_failure"] == {"stage": "retained_file_inventory", "error_class": "OSError"}
    with pytest.raises(artifacts.StaticArtifactLimit, match="accounting_unknown"):
        _ = sink.persisted_bytes
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    for kind in ("coupled_pin_step_receipt", artifacts.ACTUAL_TRIAL_FINAL_KIND):
        with pytest.raises(artifacts.StaticArtifactLimit, match="accounting_unknown"):
            sink("b"*32, {"kind": kind})
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before
    monkeypatch.setattr(artifacts.os, "write", original_write)
    monkeypatch.setattr(artifacts.os, "fstat", original_fstat)
    assert sink.reconcile_retained_bytes() is True
    assert sink.resource_status()["byte_accounting_known"] is True
    assert sink.resource_status()["model_query_allowed"] is False
    assert sink.persisted_bytes == sum(path.stat().st_size for path in root.iterdir())
    sink("c"*32, {"kind": artifacts.ACTUAL_TRIAL_FINAL_KIND, "failure": "FixtureHardDeadline"})
    assert (root/("a"*32+".json.gz")).read_bytes() == before["a"*32+".json.gz"]
    assert sink.artifact_inventory()["io_incomplete"] is True
    sink.close()


@pytest.mark.parametrize("secondary,error_class", [(OSError("fixture"), "OSError"),
    (FixtureHardDeadline(), "BaseException")])
def test_accounting_secondary_failure_is_closed_and_preserves_primary(tmp_path, monkeypatch, secondary, error_class):
    free_space(monkeypatch)
    root = tmp_path/"corpus"
    sink = artifacts.BoundedStaticArtifactSink(root)
    original_write = artifacts.os.write
    original_account = sink._account_retained_files
    primary = FixtureHardDeadline()

    def write_then_interrupt(descriptor, data):
        original_write(descriptor, data[:5])
        raise primary

    def fail_accounting():
        raise secondary

    monkeypatch.setattr(artifacts.os, "write", write_then_interrupt)
    monkeypatch.setattr(sink, "_account_retained_files", fail_accounting)
    with pytest.raises(FixtureHardDeadline) as caught:
        sink("a"*32, {"kind": "coupled_pin_step_receipt"})
    assert caught.value is primary
    assert sink.resource_status()["reconciliation_failure"] == {
        "stage": "retained_file_inventory", "error_class": error_class}
    assert sink.reconcile_retained_bytes() is False
    with pytest.raises(artifacts.StaticArtifactLimit, match="accounting_unknown"):
        sink("b"*32, {"kind": artifacts.ACTUAL_TRIAL_FINAL_KIND})
    monkeypatch.setattr(artifacts.os, "write", original_write)
    monkeypatch.setattr(sink, "_account_retained_files", original_account)
    assert sink.reconcile_retained_bytes() is True
    assert sink.persisted_bytes == sum(path.stat().st_size for path in root.iterdir())
    sink.close()
