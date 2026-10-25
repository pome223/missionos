"""Bounded parent-side reads of explicitly referenced worker artifacts.

The saved checker receives bytes keyed by their independently checked hashes;
it never opens producer paths. No directory scan, symlink traversal, arbitrary
model-supplied attachment or credential file is admitted by this loader.
"""
from hashlib import sha256
from pathlib import Path, PurePosixPath
import re

MAX_FILES = 64
MAX_FILE_BYTES = 32*1024**2
MAX_TOTAL_BYTES = 256*1024**2


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _reference(value):
    _require(type(value) is dict and set(value) in ({"path", "sha256"}, {"path", "sha256", "raw_sha256"})
        and type(value["path"]) is str and type(value["sha256"]) is str
        and re.fullmatch(r"[a-f0-9]{64}", value["sha256"])
        and ("raw_sha256" not in value or type(value["raw_sha256"]) is str
            and re.fullmatch(r"[a-f0-9]{64}", value["raw_sha256"])),
        "restored_native_artifact_reference_shape")
    name = value["path"]
    path = PurePosixPath(name)
    _require(name and "\x00" not in name and "\\" not in name and not path.is_absolute()
        and ".." not in path.parts and path.as_posix() == name and path.parts,
        "restored_native_artifact_path_escape")
    return path


def governance_references(governance):
    """Only contract-defined file fields, never a recursive arbitrary JSON walk."""
    refs = [governance["closed_origin"]["raw"]]
    for batch in governance["forecast_batches"]:
        refs.extend(batch[key] for key in ("job", "batch_claim", "profile"))
        refs.extend(batch["origins"].values())
        _require(set(batch["candidates"]) == {"nominal", "next_orbit"},
            "restored_native_artifact_candidate_scope")
        for candidate in batch["candidates"].values():
            _require(set(candidate) == {"prediction", "result", "integration", "started", "worker_claim"},
                "restored_native_artifact_worker_fields")
            refs.extend(candidate.values())
    refs.extend(change["origin"] for change in governance["current_origin_changes"])
    _require(len(refs) <= MAX_FILES, "restored_native_artifact_file_budget")
    for ref in refs:
        _reference(ref)
    return refs


def load_restored_native_artifacts(governance, root):
    root = Path(root).absolute()
    _require(root.is_dir() and not root.is_symlink() and root.resolve() == root,
        "restored_native_artifact_root_not_real_directory")
    refs, result, read_paths, total = governance_references(governance), {}, {}, 0
    for reference in refs:
        relative = _reference(reference)
        path = root
        for part in relative.parts:
            path = path/part
            _require(not path.is_symlink(), "restored_native_artifact_symlink_forbidden")
        _require(path.resolve().is_relative_to(root) and path.is_file(),
            "restored_native_artifact_not_owned_file")
        if path in read_paths:
            _require(read_paths[path] == reference["sha256"], "restored_native_artifact_path_rebound")
            continue
        size = path.stat().st_size
        _require(0 < size <= MAX_FILE_BYTES and total+size <= MAX_TOTAL_BYTES,
            "restored_native_artifact_byte_budget")
        raw = path.read_bytes()
        _require(len(raw) == size and sha256(raw).hexdigest() == reference["sha256"],
            "restored_native_artifact_bytes_changed")
        # The private, source-bound worker owns this exclusive directory. The
        # second metadata/resolve check detects ordinary replacement races; it
        # is not an OS sandbox against a hostile local account.
        _require(not path.is_symlink() and path.stat().st_size == size and path.resolve().is_relative_to(root),
            "restored_native_artifact_changed_while_reading")
        result[reference["sha256"]] = raw
        read_paths[path] = reference["sha256"]
        total += size
    return result
