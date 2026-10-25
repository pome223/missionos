"""Main-thread guard for one opt-in local run; no retries or process-wide kills.

The interval is a sampling bound, not a reservation of disk space. An interrupt
unwinds owned numerical-worker cleanup, preserving artifacts already written.
It cannot guarantee a final numerical state when interruption occurs mid-step.
"""

from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import signal
import threading
import time


class BudgetExceeded(BaseException):
    """An outer stop must not become a recoverable prediction-tool failure."""

    def __init__(self, record):
        self.record = record
        super().__init__(record["reason"])


def _tree_bytes(root):
    total = 0
    if root.is_symlink():
        raise ValueError("budget_output_symlink")
    if not root.exists():
        return 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        for name in files:
            try:
                total += (Path(directory) / name).lstat().st_size
            except FileNotFoundError:
                continue  # Atomic artifact replacement can race a size sample.
    return total


def _free_bytes(root):
    current = root
    while not current.exists():
        current = current.parent
    return shutil.disk_usage(current).free


class RuntimeBudget:
    """Check during Python execution/sleep and abort only this calling run."""

    def __init__(
        self,
        output,
        authority,
        *,
        interval_s=5.0,
        clock=time.monotonic,
        tree_bytes=_tree_bytes,
        free_bytes=_free_bytes,
    ):
        self.output = Path(output)
        self.limits = {
            key: authority[key]
            for key in ("maximum_wall_time_s", "maximum_output_bytes", "minimum_free_bytes")
        }
        if (
            any(
                type(v) not in (int, float) or not math.isfinite(v) or v <= 0
                for v in self.limits.values()
            )
            or type(interval_s) not in (int, float)
            or not 0 < interval_s <= 5.0
        ):
            raise ValueError("invalid_runtime_budget")
        self.authority_sha256 = sha256(
            json.dumps(authority, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        self.clock, self.tree_bytes, self.free_bytes = clock, tree_bytes, free_bytes
        self.interval_s, self.started, self.failure = float(interval_s), None, None
        self.active, self.last, self.previous_handler = False, None, None

    def sample(self):
        row = {
            "elapsed_s": max(0.0, self.clock() - self.started),
            "output_bytes": self.tree_bytes(self.output),
            "free_bytes": self.free_bytes(self.output),
        }
        if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in row.values()):
            raise ValueError("invalid_runtime_budget_observation")
        self.last = row
        reason = (
            "maximum_wall_time"
            if row["elapsed_s"] >= self.limits["maximum_wall_time_s"]
            else "maximum_output_bytes"
            if row["output_bytes"] >= self.limits["maximum_output_bytes"]
            else "minimum_free_bytes"
            if row["free_bytes"] < self.limits["minimum_free_bytes"]
            else None
        )
        return reason, row

    def check(self, *_signal_args):
        try:
            reason, row = self.sample()
        except (OSError, ValueError):
            reason, row = "budget_observation_unavailable", self.last
        if reason:
            # Disarm first: a second interrupt must not destroy cleanup/evidence.
            if self.active:
                signal.setitimer(signal.ITIMER_REAL, 0.0)
            self.failure = {
                "schema": "missionos.local_runtime_budget_failure.v1",
                "reason": reason,
                "limits": self.limits,
                "observation": row,
                "authority_sha256": self.authority_sha256,
                "poll_interval_s": self.interval_s,
                "retry_performed": False,
                "physical_outcome": "unresolved_interrupted",
                "physical_execution": False,
                "existing_artifacts_preserved": True,
                "final_state_not_guaranteed": True,
            }
            raise BudgetExceeded(self.failure)

    def __enter__(self):
        if threading.current_thread() is not threading.main_thread():
            raise ValueError("runtime_budget_requires_main_thread")
        if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
            raise ValueError("runtime_budget_timer_already_in_use")
        self.started = self.clock()
        self.check()
        self.previous_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, self.check)
        self.active = True
        signal.setitimer(signal.ITIMER_REAL, self.interval_s, self.interval_s)
        return self

    def __exit__(self, kind, value, tb):
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        self.active = False
        signal.signal(signal.SIGALRM, self.previous_handler)
        if kind is None:
            self.check()
        return False

    def failure_artifact(self):
        if self.failure is None:
            raise ValueError("runtime_budget_failure_unavailable")
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.output / "runtime-budget-failure.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.failure, sort_keys=True, indent=2, allow_nan=False) + "\n"
        )
        temporary.replace(target)
        return self.failure
