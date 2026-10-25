"""Scripted evidence-storage contracts; no mission, forecast or provider call."""
from copy import deepcopy
from hashlib import sha256
import gzip
import socket
import subprocess

import pytest

from src.runtime import starship_final_record_storage as storage
from src.runtime import starship_final_shared as shared


@pytest.fixture(autouse=True)
def no_external_or_physics(monkeypatch):
    from src.runtime import starship_sixdof as dyn, starship_physics as env
    def forbidden(*_a, **_kw):
        raise AssertionError("Storage contracts cannot execute physics, providers or subprocesses")
    for obj, name in ((dyn, "step"), (dyn, "_rk4"), (dyn, "_rhs"), (env, "step"),
            (socket, "socket"), (socket, "create_connection"), (subprocess, "Popen")):
        monkeypatch.setattr(obj, name, forbidden)


@pytest.fixture
def evidence(tmp_path):
    jobs = [{"index": i, "action": "continue" if i < 3 else "adopt", "sample_label": i % 3}
        for i in range(6)]
    forecasts = [{"index": i, "record": {"scripted_trace": "x"*3000}, "job": jobs[i]} for i in range(6)]
    events, refs, artifacts = [], {}, {}
    for i, forecast in enumerate(forecasts):
        folder = tmp_path/f"numerical-0-forecast/{i}"
        folder.mkdir(parents=True)
        job = shared.wire({"schema": storage.JOB_SCHEMA, "kind": "forecast", "payload": jobs[i]})
        raw = shared.wire({"schema": storage.RESULT_SCHEMA, "kind": "forecast", "job_sha256": sha256(job).hexdigest(),
            "checker_pid": 100+i, "credential_free": True, "result": forecast})
        for name, data in (("job.json", job), ("result.json.gz", gzip.compress(raw, mtime=0))):
            (folder/name).write_bytes(data)
            identity = sha256(data).hexdigest()
            refs[str((folder/name).relative_to(tmp_path))] = identity
            artifacts[identity] = data
        events += [{"event": "started", "index": i, "pid": 100+i, "time": 10.+i},
            {"event": "reaped", "index": i, "pid": 100+i, "exit_code": 0, "time": 20.+i}]
    task = {"schema": storage.OWNER_SCHEMA, "kind": "forecast", "events": events,
        "automatic_retry": False, "credential_free": True, "cancelled": False, "maximum_concurrent_children": 2}
    record = {"schema": "missionos.starship_final_native_mission.v3", "preserved_state": "opaque_actual_state",
        "sensor_return": {"forecast_batches": [{"jobs": jobs, "forecasts": forecasts,
            "status": "received", "response_sha256": shared.digest(forecasts)}]},
        "supplemental": {"numerical_tasks": [task]}}
    return record, tmp_path, artifacts, refs


def compact(evidence):
    record, folder, _, _ = evidence
    return storage.encode_record(record, folder)


def decode(value, evidence):
    return storage.decode_record(value, artifacts=evidence[2], artifact_refs=evidence[3])


def first(value):
    return value["sensor_return"]["forecast_batches"][0]["forecasts"]


def test_scaled_individual_bounds_and_compact_record_roundtrip_without_mutation(evidence, monkeypatch):
    record, _, artifacts, _ = evidence
    before = shared.wire(record)
    monkeypatch.setattr(storage, "RAW_RECORD_LIMIT", 8000)
    value = compact(evidence)
    assert len(before) > storage.RAW_RECORD_LIMIT > len(shared.wire(value))
    assert all(len(gzip.decompress(raw)) < storage.RAW_RECORD_LIMIT
        for raw in artifacts.values() if raw.startswith(b"\x1f\x8b"))
    encoded_before = shared.wire(value)
    assert shared.wire(decode(value, evidence)) == before
    assert shared.wire(record) == before and shared.wire(value) == encoded_before
    assert first(value)["forecasts_sha256"] == evidence[0]["sensor_return"]["forecast_batches"][0]["response_sha256"]


def test_actual_runner_write_restore_boundary_uses_compact_limit(evidence, monkeypatch):
    from scripts import run_starship_final_mission as runner
    record, folder, artifacts, refs = evidence
    monkeypatch.setattr(storage, "RAW_RECORD_LIMIT", 8000)
    monkeypatch.setattr(runner, "MAXIMUM_RECORD_BYTES", 8000)
    assert len(shared.wire(record)) > 8000
    runner.save_worker_record(record, folder, plan_sha256="a"*64)
    raw = runner.restore_record(folder/"record.json.gz")
    assert len(raw) < 8000
    assert storage.decode_record(shared.parse(raw), artifacts=artifacts, artifact_refs=refs) == record
    result = shared.read(folder/"worker-result.json")
    assert result["record_sha256"] == sha256(raw).hexdigest()
    assert result["plan_sha256"] == "a"*64


def test_actual_runner_rejects_oversized_compact_record_before_gzip_or_write(evidence, monkeypatch):
    from scripts import run_starship_final_mission as runner
    record, folder, _, _ = evidence
    record["unrelated_required_raw_evidence"] = "y"*9000
    monkeypatch.setattr(storage, "RAW_RECORD_LIMIT", 8000)
    monkeypatch.setattr(runner, "MAXIMUM_RECORD_BYTES", 8000)
    def forbidden(*_a, **_kw):
        raise AssertionError("The compact raw cap must run before compression")
    monkeypatch.setattr(runner.gzip, "compress", forbidden)
    with pytest.raises(ValueError, match="final_record_write_budget"):
        runner.save_worker_record(record, folder, plan_sha256="a"*64)
    assert not (folder/"record.json.gz").exists()
    assert not (folder/"worker-result.json").exists()


@pytest.mark.parametrize("change", ["state", "job", "callback", "missing_child", "owner_pid", "duplicate_pid", "bad_exit"])
def test_encoder_rejects_nonmatching_owned_evidence(evidence, change):
    record, folder, _, _ = evidence
    batch = record["sensor_return"]["forecast_batches"][0]
    if change == "state":
        batch["forecasts"][0]["record"]["scripted_trace"] += "changed"
    elif change == "job":
        batch["jobs"][0]["index"] = 99
    elif change == "callback":
        batch["response_sha256"] = "a"*64
    elif change == "missing_child":
        (folder/"numerical-0-forecast/0/result.json.gz").unlink()
    elif change == "owner_pid":
        record["supplemental"]["numerical_tasks"][0]["events"][0]["pid"] = 999
    elif change == "duplicate_pid":
        for event in record["supplemental"]["numerical_tasks"][0]["events"]:
            if event["index"] == 1:
                event["pid"] = 100
    else:
        record["supplemental"]["numerical_tasks"][0]["events"][1]["exit_code"] = 1
    with pytest.raises(ValueError):
        compact(evidence)


def test_encoder_rejects_symlink_to_other_evidence(evidence, tmp_path):
    _, folder, _, _ = evidence
    target = folder/"numerical-0-forecast/0/result.json.gz"
    copy = tmp_path/"other.gz"
    target.rename(copy)
    target.symlink_to(copy)
    with pytest.raises(ValueError, match="symlink"):
        compact(evidence)


@pytest.mark.parametrize("change", ["missing", "corrupt", "swapped", "duplicate", "escape", "orphan", "raw_hash", "payload_hash", "raw_size", "callback", "job_binding"])
def test_decoder_rejects_changed_descriptors_and_external_evidence(evidence, change):
    value = compact(evidence)
    desc = first(value)
    artifacts, refs = evidence[2:]
    if change == "missing":
        del artifacts[desc["items"][0]["gzip_sha256"]]
    elif change == "corrupt":
        identity = desc["items"][0]["gzip_sha256"]
        artifacts[identity] += b"bad"
    elif change == "swapped":
        desc["items"][0], desc["items"][1] = desc["items"][1], desc["items"][0]
    elif change == "duplicate":
        desc["items"][1] = deepcopy(desc["items"][0])
    elif change == "escape":
        desc["items"][0]["path"] = "../result.json.gz"
    elif change == "orphan":
        desc["task_index"] = 1
    elif change == "raw_hash":
        desc["items"][0]["raw_sha256"] = "a"*64
    elif change == "payload_hash":
        desc["items"][0]["payload_sha256"] = "a"*64
    elif change == "raw_size":
        desc["items"][0]["raw_bytes"] -= 1
    elif change == "callback":
        value["sensor_return"]["forecast_batches"][0]["response_sha256"] = "a"*64
    else:
        value["sensor_return"]["forecast_batches"][0]["jobs"][0]["index"] = 99
    with pytest.raises(ValueError):
        decode(value, evidence)


def test_coherently_rehashed_gzip_from_another_child_is_not_the_named_job(evidence):
    value = compact(evidence)
    desc, refs = first(value), evidence[3]
    source = desc["items"][1]
    target = desc["items"][0]
    for key in ("gzip_sha256", "raw_sha256", "raw_bytes", "payload_sha256"):
        target[key] = source[key]
    refs[target["path"]] = source["gzip_sha256"]
    with pytest.raises(ValueError, match="child_result_binding"):
        decode(value, evidence)


@pytest.mark.parametrize("raw", [
    b"not-gzip",
    gzip.compress(b"scripted", mtime=0)[:-3],
    b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03\x07"+b"\x00"*8,
    None,
], ids=["invalid-header", "truncated-footer", "invalid-deflate", "expansion-over-budget"])
def test_invalid_or_excessive_expansion_is_rejected_even_with_new_external_pin(evidence, monkeypatch, raw):
    value = compact(evidence)
    monkeypatch.setattr(storage, "RAW_RECORD_LIMIT", 8000)
    packed = raw if raw is not None else gzip.compress(b"x"*8001, mtime=0)
    item = first(value)["items"][0]
    identity = sha256(packed).hexdigest()
    evidence[2][identity] = packed
    evidence[3][item["path"]] = identity
    item["gzip_sha256"] = identity
    with pytest.raises(ValueError, match="invalid_gzip|expansion_budget"):
        decode(value, evidence)


@pytest.mark.parametrize("state", ["no_sensor", "normal", "interrupted", "submit_error"])
def test_no_forecast_or_interrupted_result_is_preserved_not_manufactured(evidence, state):
    record = evidence[0]
    if state == "no_sensor":
        record["sensor_return"] = None
        record["supplemental"]["numerical_tasks"] = []
    elif state == "normal":
        record["sensor_return"]["forecast_batches"] = []
        record["supplemental"]["numerical_tasks"] = []
    else:
        batch = record["sensor_return"]["forecast_batches"][0]
        batch.update(forecasts=None, response_sha256=None, status="expired" if state == "interrupted" else "submit_error")
        if state == "submit_error":
            record["supplemental"]["numerical_tasks"] = []
    before = shared.wire(record)
    assert shared.wire(compact(evidence)) == before
    assert shared.wire(decode(record, evidence)) == before


@pytest.mark.parametrize("shape", ["inline", "partial_list", "partial_descriptor", "extra_batch", "orphan_owner", "unsafe_ref", "total_budget", "per_file_budget"])
def test_storage_scope_and_budgets_fail_closed(evidence, monkeypatch, shape):
    record = evidence[0]
    value = compact(evidence)
    if shape == "inline":
        value = record
    elif shape == "partial_list":
        record["sensor_return"]["forecast_batches"][0]["forecasts"].pop()
        with pytest.raises(ValueError, match="complete_six"):
            compact(evidence)
        return
    elif shape == "partial_descriptor":
        first(value)["items"].pop()
    elif shape == "extra_batch":
        value["sensor_return"]["forecast_batches"].append(deepcopy(value["sensor_return"]["forecast_batches"][0]))
    elif shape == "orphan_owner":
        value["sensor_return"] = None
    elif shape == "unsafe_ref":
        evidence[3]["../escape"] = next(iter(evidence[2]))
    elif shape == "total_budget":
        monkeypatch.setattr(storage, "ARTIFACT_TOTAL_LIMIT", sum(map(len, evidence[2].values()))-1)
    else:
        raw = b"x"*8001
        evidence[2][sha256(raw).hexdigest()] = raw
        monkeypatch.setattr(storage, "RAW_RECORD_LIMIT", 8000)
    with pytest.raises(ValueError):
        decode(value, evidence)
