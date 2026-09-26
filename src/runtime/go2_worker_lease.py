"""POSIX process-lifetime fence inherited across Gateway -> simulator exec.

Never unlink lease files or explicitly LOCK_UN a shared inherited description:
close releases the lock only after both parent and child have closed/exited.
Unknown/missing/replaced leases are not evidence that a worker stopped.
"""

import os
from pathlib import Path

PROTOCOL = "go2_worker_flock.v1"


def acquire(path: Path):
    import fcntl

    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stat = os.fstat(fd)
        return fd, dict(protocol=PROTOCOL, path=str(path), device=stat.st_dev, inode=stat.st_ino)
    except BaseException:
        os.close(fd)
        raise


def stopped(lease: dict) -> bool:
    """Only a free lock on the original recorded inode proves release."""
    import fcntl

    if lease.get("protocol") != PROTOCOL:
        return False
    try:
        fd = os.open(lease["path"], os.O_RDWR | os.O_NOFOLLOW)
    except (OSError, KeyError, TypeError):
        return False
    try:
        stat = os.fstat(fd)
        if (stat.st_dev, stat.st_ino) != (lease.get("device"), lease.get("inode")):
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True
    finally:
        os.close(fd)
