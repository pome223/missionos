"""Persist strict JSON before handing an artifact to a verifier."""
from __future__ import annotations

import json
from pathlib import Path


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
