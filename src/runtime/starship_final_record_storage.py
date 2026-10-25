"""Bounded final-record storage; full forecast evidence stays in owned files.

This codec changes storage only. It neither grants authority nor replaces a
forecast, task receipt, controller history or the semantic saved validators.
The caller owns the mission schema and externally pins every artifact byte.
"""
from hashlib import sha256
import gzip
from io import BytesIO
import os
from pathlib import Path, PurePosixPath
import zlib

from . import starship_final_shared as shared

RAW_RECORD_LIMIT = 512 * 2**20
ARTIFACT_TOTAL_LIMIT = 1024 * 2**20
MAXIMUM_FORECAST_BATCHES = 1  # Guard reconsideration reuses the same six points.
MAXIMUM_TASKS = 3
DESCRIPTOR_SCHEMA = "missionos.starship_final_forecast_storage.v1"
RESULT_SCHEMA = "missionos.starship_final_numerical_result.v1"
JOB_SCHEMA = "missionos.starship_final_numerical_job.v1"
OWNER_SCHEMA = "missionos.starship_final_numerical_task_owner.v1"
DESCRIPTOR_KEYS = {"schema", "batch_index", "task_index", "forecasts_sha256", "items"}
ITEM_KEYS = {"index", "path", "gzip_sha256", "raw_sha256", "raw_bytes", "job_sha256", "payload_sha256"}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _is_hash(value):
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _safe_reference(name):
    if type(name) is not str or not name or "\x00" in name or "\\" in name:
        return False
    path = PurePosixPath(name)
    return bool(path.parts) and not path.is_absolute() and ".." not in path.parts and path.as_posix() == name


def _list_digest(rows):
    """Canonical list identity without allocating another combined trajectory."""
    identity = sha256(b"[")
    for index, row in enumerate(rows):
        if index:
            identity.update(b",")
        identity.update(shared.wire(row))
    identity.update(b"]")
    return identity.hexdigest()


def _context(record):
    _require(type(record) is dict, "final_storage_record_required")
    sensor = record.get("sensor_return")
    tasks = record.get("supplemental", {}).get("numerical_tasks", [])
    _require(type(tasks) is list and len(tasks) <= MAXIMUM_TASKS,
        "final_storage_task_budget")
    forecast_tasks = [(index, task) for index, task in enumerate(tasks)
        if type(task) is dict and task.get("kind") == "forecast"]
    if sensor is None:
        _require(not forecast_tasks, "final_storage_orphan_forecast_owner")
        return None, [], forecast_tasks
    _require(type(sensor) is dict and type(sensor.get("forecast_batches")) is list
        and len(sensor["forecast_batches"]) <= MAXIMUM_FORECAST_BATCHES,
        "final_storage_forecast_batch_budget")
    batches = sensor["forecast_batches"]
    _require(all(type(batch) is dict and "forecasts" in batch for batch in batches)
        and len(forecast_tasks) <= len(batches), "final_storage_orphan_forecast_owner")
    return sensor, batches, forecast_tasks


def _owner(batch_index, forecast_tasks):
    _require(type(batch_index) is int and batch_index < len(forecast_tasks),
        "final_storage_completed_batch_missing_owner")
    task_index, owner = forecast_tasks[batch_index]
    _require(owner.get("schema") == OWNER_SCHEMA and owner.get("kind") == "forecast"
        and owner.get("automatic_retry") is False and owner.get("credential_free") is True
        and owner.get("cancelled") is False and owner.get("maximum_concurrent_children") == 2,
        "final_storage_completed_forecast_owner_changed")
    events = owner.get("events")
    _require(type(events) is list and all(type(row) is dict for row in events),
        "final_storage_owner_events_required")
    starts = [row for row in events if row.get("event") == "started"]
    exits = [row for row in events if row.get("event") == "reaped"]
    _require(len(starts) == len(exits) == 6
        and all(type(row.get("index")) is int for row in starts+exits)
        and {row["index"] for row in starts} == {row["index"] for row in exits} == set(range(6)),
        "final_storage_six_once_only_children_required")
    pids = []
    for index in range(6):
        start = next(row for row in starts if row["index"] == index)
        exit_row = next(row for row in exits if row["index"] == index)
        pid = start.get("pid")
        _require(type(pid) is int and pid > 0 and exit_row.get("pid") == pid
            and type(exit_row.get("exit_code")) is int and exit_row["exit_code"] == 0,
            "final_storage_child_exit_or_pid_changed")
        pids.append(pid)
    _require(len(set(pids)) == 6, "final_storage_duplicate_child_identity")
    return task_index, pids


def _paths(task_index, index):
    base = f"numerical-{task_index}-forecast/{index}/"
    return base+"job.json", base+"result.json.gz"


def _read_owned(folder, name):
    root = Path(folder).absolute()
    _require(root.is_dir() and not root.is_symlink() and root.resolve() == root,
        "final_storage_owned_real_folder_required")
    path = root
    for component in Path(name).parts:
        path = path/component
        _require(not path.is_symlink(), "final_storage_symlink_forbidden")
    _require(path.resolve().is_relative_to(root) and path.is_file(), "final_storage_owned_file_required")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        _require(os.fstat(stream.fileno()).st_size <= RAW_RECORD_LIMIT, "final_storage_file_budget")
        raw = stream.read(RAW_RECORD_LIMIT+1)
    _require(len(raw) <= RAW_RECORD_LIMIT and not path.is_symlink(), "final_storage_file_budget")
    return raw


def _inflate(packed, *, expected_size=None):
    _require(type(packed) is bytes and 0 < len(packed) <= RAW_RECORD_LIMIT,
        "final_storage_compressed_budget")
    try:
        with gzip.GzipFile(fileobj=BytesIO(packed)) as stream:
            raw = stream.read(RAW_RECORD_LIMIT+1)
    except (OSError, EOFError, zlib.error):
        raise ValueError("final_storage_invalid_gzip") from None
    _require(0 < len(raw) <= RAW_RECORD_LIMIT, "final_storage_expansion_budget")
    _require(expected_size is None or len(raw) == expected_size, "final_storage_raw_size_changed")
    return raw


def _child(job_raw, raw, batch, index, pid):
    jobs = batch.get("jobs")
    _require(type(jobs) is list and len(jobs) == 6, "final_storage_six_source_jobs_required")
    expected_job = {"schema": JOB_SCHEMA, "kind": "forecast", "payload": jobs[index]}
    _require(shared.wire(shared.parse(job_raw)) == shared.wire(expected_job),
        "final_storage_child_job_not_current_batch")
    child = shared.parse(raw)
    _require(type(child) is dict and set(child) == {
        "schema", "kind", "job_sha256", "checker_pid", "credential_free", "result"}
        and child["schema"] == RESULT_SCHEMA and child["kind"] == "forecast"
        and child["job_sha256"] == sha256(job_raw).hexdigest()
        and type(child["checker_pid"]) is int and child["checker_pid"] == pid
        and child["credential_free"] is True and type(child["result"]) is dict,
        "final_storage_child_result_binding_changed")
    return child


def _replace(record, sensor, batches):
    if sensor is None:
        return dict(record)
    return {**record, "sensor_return": {**sensor, "forecast_batches": batches}}


def encode_record(record, folder):
    """Replace complete six-point lists with exact existing child references.

    No files are written, no input object is mutated, and interrupted ``None``
    results remain ``None``. The caller must cap compact raw bytes BEFORE gzip.
    """
    sensor, batches, owners = _context(record)
    encoded = []
    for batch_index, batch in enumerate(batches):
        forecasts = batch["forecasts"]
        if forecasts is None:
            encoded.append(dict(batch))
            continue
        _require(type(forecasts) is list and len(forecasts) == 6,
            "final_storage_complete_six_forecasts_required")
        task_index, pids = _owner(batch_index, owners)
        items = []
        for index, forecast in enumerate(forecasts):
            job_path, path = _paths(task_index, index)
            job_raw, packed = _read_owned(folder, job_path), _read_owned(folder, path)
            raw = _inflate(packed)
            child = _child(job_raw, raw, batch, index, pids[index])
            _require(shared.wire(child["result"]) == shared.wire(forecast),
                "final_storage_list_differs_from_owned_child")
            items.append({"index": index, "path": path, "gzip_sha256": sha256(packed).hexdigest(),
                "raw_sha256": sha256(raw).hexdigest(), "raw_bytes": len(raw),
                "job_sha256": child["job_sha256"], "payload_sha256": shared.digest(child["result"])})
        identity = _list_digest(forecasts)
        _require(batch.get("response_sha256") == identity, "final_storage_callback_list_digest_changed")
        descriptor = {"schema": DESCRIPTOR_SCHEMA, "batch_index": batch_index, "task_index": task_index,
            "forecasts_sha256": identity, "items": items}
        encoded.append({**batch, "forecasts": descriptor})
    return _replace(record, sensor, encoded)


def decode_record(record, *, artifacts, artifact_refs):
    """Restore exact semantic lists using externally pinned bytes, no I/O.

    This does not impose the outer raw-record cap on the expanded semantic
    object. Each retained file stays bounded, and all ordinary semantic/task/
    guard validators must run AFTER this codec succeeds.
    """
    _require(type(artifacts) is dict and type(artifact_refs) is dict
        and all(_is_hash(identity) and type(raw) is bytes and len(raw) <= RAW_RECORD_LIMIT
            and sha256(raw).hexdigest() == identity for identity, raw in artifacts.items())
        and sum(len(raw) for raw in artifacts.values()) <= ARTIFACT_TOTAL_LIMIT
        and all(_safe_reference(name) and _is_hash(identity) and identity in artifacts
            for name, identity in artifact_refs.items()),
        "final_storage_external_artifact_map_budget_or_hash")
    sensor, batches, owners = _context(record)
    decoded, used_paths = [], set()

    def pinned(path):
        identity = artifact_refs.get(path)
        _require(_is_hash(identity) and identity in artifacts, "final_storage_external_artifact_missing")
        return identity, artifacts[identity]

    for batch_index, batch in enumerate(batches):
        descriptor = batch["forecasts"]
        if descriptor is None:
            decoded.append(dict(batch))
            continue
        _require(type(descriptor) is dict and set(descriptor) == DESCRIPTOR_KEYS
            and descriptor.get("schema") == DESCRIPTOR_SCHEMA
            and type(descriptor["batch_index"]) is int and descriptor["batch_index"] == batch_index
            and type(descriptor["items"]) is list and len(descriptor["items"]) == 6
            and _is_hash(descriptor["forecasts_sha256"]), "final_storage_compact_descriptor_required")
        task_index, pids = _owner(batch_index, owners)
        _require(type(descriptor["task_index"]) is int and descriptor["task_index"] == task_index,
            "final_storage_descriptor_owner_reassigned")
        forecasts = []
        for index, item in enumerate(descriptor["items"]):
            job_path, path = _paths(task_index, index)
            _require(type(item) is dict and set(item) == ITEM_KEYS
                and type(item["index"]) is int and item["index"] == index
                and item["path"] == path and path not in used_paths
                and all(_is_hash(item[key]) for key in ("gzip_sha256", "raw_sha256", "job_sha256", "payload_sha256"))
                and type(item["raw_bytes"]) is int and 0 < item["raw_bytes"] <= RAW_RECORD_LIMIT,
                "final_storage_descriptor_item_path_order_or_budget")
            used_paths.add(path)
            job_identity, job_raw = pinned(job_path)
            gzip_identity, packed = pinned(path)
            _require(job_identity == item["job_sha256"] and gzip_identity == item["gzip_sha256"],
                "final_storage_external_pin_differs_from_descriptor")
            raw = _inflate(packed, expected_size=item["raw_bytes"])
            _require(sha256(raw).hexdigest() == item["raw_sha256"], "final_storage_raw_digest_changed")
            child = _child(job_raw, raw, batch, index, pids[index])
            _require(child["job_sha256"] == item["job_sha256"]
                and shared.digest(child["result"]) == item["payload_sha256"],
                "final_storage_payload_digest_changed")
            forecasts.append(child["result"])
        identity = _list_digest(forecasts)
        _require(identity == descriptor["forecasts_sha256"] == batch.get("response_sha256"),
            "final_storage_restored_callback_list_digest_changed")
        decoded.append({**batch, "forecasts": forecasts})
    return _replace(record, sensor, decoded)
