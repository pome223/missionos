"""Narrow Darwin Python.app startup metadata handling for offline workers.

The parent deliberately does not inherit this key. The Darwin runtime may add
it before Python executes. Validate its current-user numeric encoding form in a
local copy only; callers retain their own exact credential/environment guards.
No values are logged, no unknown key is dropped and os.environ is unchanged.
"""
import os
import re
import sys

_METADATA = "__CF_USER_TEXT_ENCODING"
_ENCODING = re.compile(r"0x([0-9A-Fa-f]{1,8}):0x([0-9A-Fa-f]{1,8}):0x([0-9A-Fa-f]{1,8})\Z")


def worker_environment_view(environ=None):
    """Return a detached view with only validated Darwin startup metadata gone."""
    actual = dict(os.environ if environ is None else environ)
    if _METADATA in actual:
        value = actual[_METADATA]
        match = _ENCODING.fullmatch(value) if type(value) is str else None
        if sys.platform != "darwin" or match is None or int(match[1],16) != os.getuid():
            raise ValueError("restored_worker_darwin_encoding_metadata_invalid")
        del actual[_METADATA]
    return actual
