"""Persist strict JSON before handing an artifact to a verifier."""
from __future__ import annotations

import gzip
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import re
import stat

CHECKPOINT_SCHEMA = "missionos.starship_compressed_checkpoint.v1"
MAX_CHECKPOINT_JSON_BYTES = 1_200_000_000
MAX_CHECKPOINT_STORED_BYTES = MAX_CHECKPOINT_JSON_BYTES + 1_000_000


def write_verified_input(path: Path, value):
    """Return the stored JSON value, including tuple-to-array conversion.

    Serialize before opening so invalid numbers/types leave no partial file.
    Exclusive creation preserves earlier evidence. Verifiers remain strict;
    producers, rather than verifiers, own the serialization boundary.
    """
    serialized = json.dumps(value, indent=2, allow_nan=False) + "\n"
    with path.open("x", encoding="utf-8") as stream:
        stream.write(serialized)
    return json.loads(path.read_text(encoding="utf-8"))


def _regular_bytes(path, maximum):
    """Bound reads and refuse symlinks/non-files, including compressed payloads."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise ValueError("checkpoint_file_type_or_size")
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("checkpoint_file_size")
    return raw


def read_verified_input(path: Path):
    """Read ordinary JSON or an explicitly hash-bound local gzip checkpoint.

    Canonical outer studies remain ordinary self-contained JSON. Checkpoints
    are not trust roots: their hashes detect corruption but require the usual
    approved source binding and independent study verification.
    """
    path = Path(path)
    value = json.loads(_regular_bytes(path, MAX_CHECKPOINT_JSON_BYTES))
    if not isinstance(value, dict) or value.get("schema") != CHECKPOINT_SCHEMA:
        return value
    if (set(value) != {"schema", "encoding", "artifact", "stored_sha256", "stored_bytes", "json_sha256", "json_bytes"}
            or value["encoding"] != "gzip_json"
            or value["artifact"] != path.name + ".gz"
            or any(type(value[k]) is not int or not 0 < value[k] <= limit
                for k, limit in (("json_bytes", MAX_CHECKPOINT_JSON_BYTES),
                                 ("stored_bytes", MAX_CHECKPOINT_STORED_BYTES)))
            or any(type(value[k]) is not str or re.fullmatch(r"[0-9a-f]{64}", value[k]) is None
                for k in ("stored_sha256", "json_sha256"))):
        raise ValueError("checkpoint_reference_invalid")
    # Exact sibling basename above disallows absolute paths, traversal and
    # chained references. O_NOFOLLOW also rejects a symlink at that basename.
    stored = _regular_bytes(path.parent / value["artifact"], value["stored_bytes"])
    if len(stored) != value["stored_bytes"] or sha256(stored).hexdigest() != value["stored_sha256"]:
        raise ValueError("checkpoint_stored_hash_or_length")
    with gzip.GzipFile(fileobj=io.BytesIO(stored)) as stream:
        raw = stream.read(value["json_bytes"] + 1)
    if len(raw) != value["json_bytes"] or sha256(raw).hexdigest() != value["json_sha256"]:
        raise ValueError("checkpoint_json_hash_or_length")
    return json.loads(raw)


def write_verified_checkpoint(path: Path, value):
    """Save a duplicate checkpoint losslessly, publishing its reference last.

    No previous file is overwritten or removed. Interrupted writes retain their
    bytes and never advertise a completed checkpoint. Both representations are
    bounded and checked; the caller still writes the final study as normal JSON.
    """
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise FileExistsError(path)
    raw = (json.dumps(value, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > MAX_CHECKPOINT_JSON_BYTES:
        raise ValueError("checkpoint_json_size")
    stored = gzip.compress(raw, compresslevel=1, mtime=0)
    if len(stored) > MAX_CHECKPOINT_STORED_BYTES:
        raise ValueError("checkpoint_stored_size")
    payload = path.with_name(path.name + ".gz")
    reference = {"schema": CHECKPOINT_SCHEMA, "encoding": "gzip_json", "artifact": payload.name,
        "stored_sha256": sha256(stored).hexdigest(), "stored_bytes": len(stored),
        "json_sha256": sha256(raw).hexdigest(), "json_bytes": len(raw)}
    with payload.open("xb") as stream:
        stream.write(stored)
        stream.flush()
        os.fsync(stream.fileno())
    pending = path.with_name("." + path.name + ".pending")
    with pending.open("xb") as stream:
        stream.write((json.dumps(reference, indent=2, allow_nan=False) + "\n").encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    # Link is an atomic no-replace publication, unlike rename/replace. The
    # completion marker cannot precede a fully written and durable payload.
    parent_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent_fd)
        os.link(pending, path, follow_symlinks=False)
        os.fsync(parent_fd)
        pending.unlink()
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return read_verified_input(path)
