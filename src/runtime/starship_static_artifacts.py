"""Exclusive, size-bounded evidence sink for static development queries.

Gzip storage and its durable resource ledger share the same incremental cap.
No artifact is replaced or deleted, including a partially written artifact.
The sink does not call a model, integrate a state, or establish source trust.
The serialized byte cap is checked after serialization and is not a RAM cap.
"""
from __future__ import annotations

import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
from threading import Lock

MAXIMUM_STORED_BYTES = 100*1024*1024
MINIMUM_FREE_BYTES = 400*1024*1024
SUMMARY_RESERVE_BYTES = 10*1024*1024
MAXIMUM_RAW_JSON_BYTES = 64*1024*1024
FINAL_KIND = "coupled_static_complete_or_partial"
ACTUAL_TRIAL_FINAL_KIND = "coupled_pin_trial_complete_or_partial"
FINAL_KINDS = frozenset({FINAL_KIND, ACTUAL_TRIAL_FINAL_KIND})
LEDGER_NAME = "resource-ledger.jsonl"
MAXIMUM_INVENTORY_ENTRIES = 2*17271+3


class StaticArtifactLimit(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise StaticArtifactLimit(reason)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class BoundedStaticArtifactSink:
    """One fresh corpus, retaining all evidence and reserving final-summary room."""

    def __init__(self, root, *, maximum_stored_bytes=MAXIMUM_STORED_BYTES,
                 summary_reserve_bytes=SUMMARY_RESERVE_BYTES, minimum_free_bytes=MINIMUM_FREE_BYTES):
        _require(type(maximum_stored_bytes) is int and 0 < maximum_stored_bytes <= MAXIMUM_STORED_BYTES
            and type(summary_reserve_bytes) is int and 0 < summary_reserve_bytes < maximum_stored_bytes
            and type(minimum_free_bytes) is int and minimum_free_bytes >= MINIMUM_FREE_BYTES,
            "static_artifact_resource_bounds_required")
        self._root = Path(root)
        _require(not self._root.exists() and not self._root.is_symlink(), "fresh_static_artifact_directory_required")
        self._root.mkdir(mode=0o700, parents=False, exist_ok=False)
        metadata = self._root.stat(follow_symlinks=False)
        _require(stat.S_ISDIR(metadata.st_mode), "static_artifact_directory_required")
        self._inode = metadata.st_dev, metadata.st_ino
        self._maximum, self._reserve, self._minimum_free = maximum_stored_bytes, summary_reserve_bytes, minimum_free_bytes
        self._stored, self._ledger_bytes, self._artifact_bytes, self._artifacts = 0, 0, 0, 0
        self._ledger, self._used, self._closed, self._stop_reason, self._lock = [], set(), False, None, Lock()
        self._recorded_artifacts, self._incomplete_artifacts, self._ledger_incomplete = set(), {}, False
        self._write_sync_incomplete = False
        self._reconciliation_incomplete, self._reconciliation_failure = False, None
        self._handles_closed = False
        self._directory_fd = os.open(self._root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        self._ledger_fd = os.open(LEDGER_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                                  0o600, dir_fd=self._directory_fd)
        try:
            self._append({"kind": "resource_policy", "maximum_stored_bytes": self._maximum,
                "summary_reserve_bytes": self._reserve, "minimum_free_bytes": self._minimum_free,
                "maximum_raw_json_bytes": MAXIMUM_RAW_JSON_BYTES, "all_incremental_persisted_bytes_counted": True,
                "overwrite_allowed": False, "deletion_allowed": False, "retry_allowed": False}, final=True)
            os.fsync(self._directory_fd)
        except BaseException:
            # Initialization may fail before the caller receives an object.
            # Release its handles while preserving even a partial ledger.
            os.close(self._ledger_fd)
            os.close(self._directory_fd)
            self._ledger_fd = self._directory_fd = None
            self._handles_closed = True
            raise

    @property
    def persisted_bytes(self):
        with self._lock:
            _require(not self._reconciliation_incomplete, "static_artifact_byte_accounting_unknown")
            return self._stored

    def _free(self):
        metadata = self._root.stat(follow_symlinks=False)
        _require(not self._root.is_symlink() and stat.S_ISDIR(metadata.st_mode)
            and (metadata.st_dev, metadata.st_ino) == self._inode, "static_artifact_directory_changed")
        return shutil.disk_usage(self._root).free

    def _write(self, descriptor, data, *, ledger):
        initial_size = os.fstat(descriptor).st_size
        position = 0
        pending_failure = None
        try:
            while position < len(data):
                written = os.write(descriptor, data[position:])
                _require(written > 0, "static_artifact_short_write")
                position += written
        except BaseException as exc:
            pending_failure = exc
            raise
        finally:
            # A signal can arrive after the kernel wrote bytes but before
            # os.write returned. File length, rather than its return value,
            # accounts for every retained byte on that interrupted path.
            try:
                retained = os.fstat(descriptor).st_size-initial_size
                self._stored += retained
                if ledger:
                    self._ledger_bytes += retained
                else:
                    self._artifact_bytes += retained
            except BaseException as exc:
                self._mark_reconciliation_failure("write_end_fstat", exc)
                if pending_failure is None:
                    raise
            try:
                os.fsync(descriptor)
            except BaseException:
                self._write_sync_incomplete = True
                if pending_failure is None:
                    raise

    def _account_retained_files(self):
        """Reconcile counters after an interruption, without writing or retrying."""
        ledger_bytes = os.fstat(self._ledger_fd).st_size
        artifact_bytes = sum(os.stat(identifier+".json.gz", dir_fd=self._directory_fd,
            follow_symlinks=False).st_size for identifier in self._used)
        self._ledger_bytes, self._artifact_bytes = ledger_bytes, artifact_bytes
        self._stored = self._ledger_bytes+self._artifact_bytes

    def _mark_reconciliation_failure(self, stage, exc):
        # These fields have closed classes/stages; arbitrary error messages
        # and custom exception names are not persisted as provenance.
        error_class = ("OSError" if isinstance(exc, OSError) else
            "StaticArtifactLimit" if isinstance(exc, StaticArtifactLimit) else
            "Exception" if isinstance(exc, Exception) else "BaseException")
        self._reconciliation_incomplete = True
        self._reconciliation_failure = {"stage": stage, "error_class": error_class}

    def _try_read_only_reconciliation(self):
        try:
            self._account_retained_files()
        except BaseException as exc:
            self._mark_reconciliation_failure("retained_file_inventory", exc)
            return False
        self._reconciliation_incomplete = False
        return True

    def reconcile_retained_bytes(self):
        """Read-only counter recovery; never rewrites or retries partial evidence."""
        with self._lock:
            _require(not self._handles_closed, "static_artifact_already_closed")
            return self._try_read_only_reconciliation()

    def _append(self, value, *, final=False):
        _require(not self._reconciliation_incomplete, "static_artifact_byte_accounting_unknown")
        data = _canonical(value)+b"\n"
        limit = self._maximum if final else self._maximum-self._reserve
        _require(self._stored+len(data) <= limit, "static_artifact_ledger_byte_cap")
        _require(self._free() >= self._minimum_free+len(data), "static_artifact_free_space_guard")
        try:
            self._write(self._ledger_fd, data, ledger=True)
        except BaseException:
            self._ledger_incomplete = True
            self._try_read_only_reconciliation()
            raise
        self._ledger.append(json.loads(data))

    def _status(self):
        free = self._free()
        return {"maximum_stored_bytes": self._maximum, "summary_reserve_bytes": self._reserve,
            "minimum_free_bytes": self._minimum_free,
            "persisted_bytes": self._stored if not self._reconciliation_incomplete else None,
            "artifact_bytes": self._artifact_bytes if not self._reconciliation_incomplete else None,
            "ledger_bytes": self._ledger_bytes if not self._reconciliation_incomplete else None,
            "artifact_count": self._artifacts, "free_bytes": free,
            "free_space_guard_passed": free >= self._minimum_free,
            "query_storage_available": not self._reconciliation_incomplete
                and self._stored < self._maximum-self._reserve,
            "model_query_allowed": not self._closed and not self._handles_closed and self._stop_reason is None
                and not self._reconciliation_incomplete
                and free >= self._minimum_free and self._stored < self._maximum-self._reserve,
            "byte_accounting_known": not self._reconciliation_incomplete,
            "reconciliation_incomplete": self._reconciliation_incomplete,
            "reconciliation_failure": self._reconciliation_failure,
            "final_summary_written": self._closed, "handles_closed": self._handles_closed,
            "stop_reason": self._stop_reason}

    def resource_status(self):
        with self._lock:
            return self._status()

    def resource_ledger(self):
        with self._lock:
            return json.loads(_canonical(self._ledger))

    def artifact_inventory(self):
        """Inspect retained files; this never repairs an orphan or partial file.

        Coverage against this instance's ledger is only an in-memory assertion.
        An actual runner must independently parse disk ledger rows, match all
        disk entries, and verify compressed/raw hashes before checking a corpus.
        """
        with self._lock:
            self._free()
            entries, orphaned, unexpected, names, total = [], [], [], set(), 0
            with os.scandir(self._root) as scan:
                for index, entry in enumerate(scan):
                    _require(index < MAXIMUM_INVENTORY_ENTRIES, "static_artifact_inventory_entry_cap")
                    metadata = entry.stat(follow_symlinks=False)
                    _require(not entry.is_symlink() and stat.S_ISREG(metadata.st_mode),
                        "static_artifact_inventory_nonregular_entry")
                    is_artifact = re.fullmatch(r"[0-9a-f]{32}\.json\.gz", entry.name) is not None
                    known_name = is_artifact or entry.name == LEDGER_NAME
                    item = {"name": entry.name, "bytes": metadata.st_size, "expected_name": known_name}
                    names.add(entry.name)
                    total += metadata.st_size
                    if known_name:
                        _require(metadata.st_size <= self._maximum, "static_artifact_inventory_file_cap")
                        digest = sha256()
                        descriptor = os.open(entry.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                        with os.fdopen(descriptor, "rb") as file:
                            opened = os.fstat(file.fileno())
                            _require(stat.S_ISREG(opened.st_mode) and opened.st_size == metadata.st_size,
                                "static_artifact_inventory_file_changed")
                            read_bytes = 0
                            while chunk := file.read(1024*1024):
                                read_bytes += len(chunk)
                                _require(read_bytes <= self._maximum, "static_artifact_inventory_file_cap")
                                digest.update(chunk)
                            _require(read_bytes == metadata.st_size, "static_artifact_inventory_file_changed")
                        item["sha256"] = digest.hexdigest()
                    else:
                        unexpected.append(entry.name)
                    if is_artifact and entry.name not in self._recorded_artifacts:
                        orphaned.append(entry.name)
                    item["orphaned_or_partial"] = entry.name in orphaned
                    if entry.name in self._incomplete_artifacts:
                        item["io_failure"] = self._incomplete_artifacts[entry.name]
                    entries.append(item)
            missing = self._recorded_artifacts-names
            missing_ledger = LEDGER_NAME not in names
            self._free()
            return {"schema": "missionos.starship_static_artifact_inventory.v1",
                "entries": sorted(entries, key=lambda item: item["name"]),
                "orphaned_or_partial_artifact_names": sorted(orphaned),
                "unexpected_entry_names": sorted(unexpected), "missing_recorded_artifact_names": sorted(missing),
                "missing_resource_ledger": missing_ledger,
                "ledger_write_incomplete": self._ledger_incomplete,
                "write_sync_incomplete": self._write_sync_incomplete,
                "reconciliation_incomplete": self._reconciliation_incomplete,
                "reconciliation_failure": self._reconciliation_failure,
                "io_incomplete": self._ledger_incomplete or self._write_sync_incomplete or self._reconciliation_incomplete
                    or bool(orphaned) or bool(unexpected) or bool(missing) or missing_ledger,
                "bytes_from_regular_files": total,
                "persisted_bytes_accounted": self._stored if not self._reconciliation_incomplete else None,
                "ledger_coverage_is_in_memory_assertion": True,
                "filesystem_durability_authenticated": False}

    def check_before_query(self):
        """Durably check resources before a producer begins a new query batch."""
        with self._lock:
            status = self._status()
            _require(status["model_query_allowed"], self._stop_reason or "static_query_resources_unavailable")
            try:
                self._append({"kind": "before_query_resource_check", **status})
            except BaseException as exc:
                self._stop_reason = str(exc) if isinstance(exc, StaticArtifactLimit) else type(exc).__name__
                raise
            return self._status()

    def __call__(self, identifier, payload):
        _require(type(identifier) is str and re.fullmatch(r"[0-9a-f]{32}", identifier) is not None,
            "opaque_static_artifact_identity_required")
        _require(type(payload) is dict, "static_artifact_object_required")
        raw = _canonical(payload)
        _require(len(raw) <= MAXIMUM_RAW_JSON_BYTES, "static_artifact_raw_json_cap")
        stored = gzip.compress(raw, compresslevel=6, mtime=0)
        final = payload.get("kind") in FINAL_KINDS
        manifest = {"artifact_id": identifier, "format": "json.gz", "relative_path": identifier+".json.gz",
            "sha256": sha256(stored).hexdigest(), "raw_json_sha256": sha256(raw).hexdigest(),
            "bytes": len(stored), "persisted_before_analysis": True}
        with self._lock:
            _require(not self._closed and not self._handles_closed and identifier not in self._used,
                "static_artifact_already_written_or_closed")
            _require(not self._reconciliation_incomplete, "static_artifact_byte_accounting_unknown")
            _require(final or self._stop_reason is None, "static_artifact_stopped_without_retry")
            ledger_row = {"kind": "artifact_stored", "manifest": manifest, "final_summary": final,
                "persisted_bytes_before_write": self._stored, "payload_kind": payload.get("kind")}
            ledger_data = _canonical(ledger_row)+b"\n"
            limit = self._maximum if final else self._maximum-self._reserve
            try:
                _require(self._stored+len(stored)+len(ledger_data) <= limit, "static_artifact_stored_byte_cap")
                _require(self._free() >= self._minimum_free+len(stored)+len(ledger_data), "static_artifact_free_space_guard")
                descriptor = os.open(identifier+".json.gz", os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=self._directory_fd)
                self._used.add(identifier)
                try:
                    self._write(descriptor, stored, ledger=False)
                finally:
                    os.close(descriptor)
                self._artifacts += 1
                self._append(ledger_row, final=final)
                os.fsync(self._directory_fd)
                self._recorded_artifacts.add(manifest["relative_path"])
            except BaseException as exc:
                self._stop_reason = str(exc) if isinstance(exc, StaticArtifactLimit) else type(exc).__name__
                if identifier in self._used and manifest["relative_path"] not in self._recorded_artifacts:
                    self._incomplete_artifacts[manifest["relative_path"]] = self._stop_reason
                self._try_read_only_reconciliation()
                try:
                    os.fsync(self._directory_fd)
                except BaseException:
                    self._write_sync_incomplete = True
                raise
            if final:
                self._closed = True
            return manifest

    def close(self):
        """Release descriptors, preserving all full and partial evidence files."""
        with self._lock:
            self._handles_closed = True
            if self._ledger_fd is not None:
                os.close(self._ledger_fd)
                self._ledger_fd = None
            if self._directory_fd is not None:
                os.close(self._directory_fd)
                self._directory_fd = None
