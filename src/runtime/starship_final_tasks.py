"""Owned numerical child tasks; immediate Future API, no provider or retry.

Creating a batch is an opt-in numerical operation. At most two copied forecasts
run concurrently; a guard is a separate saved-only process. Private snapshots
travel through hash-bound files, never an observation/provider channel.
"""
from hashlib import sha256
import gzip
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from . import starship_final_shared as shared

TOKEN = "MISSIONOS_STARSHIP_FINAL_NUMERICAL_JOB"
MAXIMUM_BYTES = 512*2**20
ENV_KEYS = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT"}


def environment(token):
    return {k: v for k, v in os.environ.items() if k in ENV_KEYS} | {
        TOKEN: token, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def read_result(path):
    path = Path(path)
    shared.require(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAXIMUM_BYTES,
        "final_task_result_missing_or_oversized")
    with gzip.open(path, "rb") as stream:
        raw = stream.read(MAXIMUM_BYTES+1)
    shared.require(len(raw) <= MAXIMUM_BYTES, "final_task_result_expansion_budget")
    return shared.parse(raw)


class NumericalFuture:
    """Pollable, file-preserving process group owner, never a thread timeout."""
    def __init__(self, directory, kind, payload, *, deadline_monotonic, clock=time.monotonic):
        shared.require(kind in ("forecast", "guard") and callable(clock)
            and deadline_monotonic > clock(), "final_task_kind_or_deadline")
        self.directory, self.kind, self.clock = Path(directory), kind, clock
        self.deadline, self.cancelled = deadline_monotonic, False
        self.directory.mkdir(exist_ok=False)
        jobs = payload["jobs"] if kind == "forecast" else [payload]
        shared.require(type(jobs) is list and len(jobs) == (6 if kind == "forecast" else 1), "final_task_registered_job_count")
        self.jobs, self.running, self.finished, self.events = [], {}, {}, []
        for index, item in enumerate(jobs):
            folder = self.directory/str(index)
            folder.mkdir()
            job = {"schema": "missionos.starship_final_numerical_job.v1", "kind": kind, "payload": item}
            shared.save(folder/"job.json", job)
            token = sha256((folder/"job.json").read_bytes()).hexdigest()
            self.jobs.append({"folder": folder, "token": token, "index": index})
        try:
            self._pump()
        except BaseException:
            self.cancel()
            raise

    def _start(self, item):
        log = (item["folder"]/"worker.log").open("xb")
        try:
            process = subprocess.Popen([sys.executable, "-m", __name__, "--worker", str(item["folder"]), item["token"]],
                cwd=shared.ROOT, env=environment(item["token"]), stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True)
        finally:
            log.close()
        self.running[item["index"]] = {**item, "process": process}
        self.events.append({"event": "started", "index": item["index"], "pid": process.pid, "time": self.clock()})

    def _pump(self):
        if self.cancelled:
            return
        if self.clock() >= self.deadline:
            self.cancel()
            raise TimeoutError("final_numerical_task_deadline_no_retry")
        for index, row in list(self.running.items()):
            code = row["process"].poll()
            if code is not None:
                row["process"].wait()
                self.finished[index] = row
                del self.running[index]
                self.events.append({"event": "reaped", "index": index, "pid": row["process"].pid,
                    "exit_code": code, "time": self.clock()})
                if code != 0:
                    self.cancel()
                    raise ValueError("final_numerical_child_failed_no_retry")
        try:
            while self.jobs and len(self.running) < (2 if self.kind == "forecast" else 1):
                self._start(self.jobs.pop(0))
        except BaseException:
            self.cancel()
            raise

    def done(self):
        self._pump()
        return not self.jobs and not self.running

    def result(self):
        shared.require(not self.cancelled and self.done(), "final_numerical_future_not_complete")
        results = []
        for index in sorted(self.finished):
            row = self.finished[index]
            value = read_result(row["folder"]/"result.json.gz")
            shared.require(value["job_sha256"] == row["token"] and value["kind"] == self.kind
                and value["checker_pid"] == row["process"].pid and value["credential_free"] is True,
                "final_numerical_child_result_binding")
            results.append(value)
        if self.kind == "forecast":
            return [value["result"] for value in results]
        row = results[0]
        return {"receipt": row["result"], "process_evidence": {"checker_pid": row["checker_pid"],
            "exit_code": 0, "credential_free": True, "fixture_only": False,
            "job_sha256": row["job_sha256"], "source_bound_saved_checker": True}}

    def cancel(self):
        if self.cancelled:
            return False
        self.cancelled = True
        self.jobs.clear()
        for row in self.running.values():
            process = row["process"]
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for row in self.running.values():
            process = row["process"]
            try:
                process.wait(timeout=2.)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2.)
        self.events.append({"event": "cancelled", "time": self.clock(), "new_job_started": False})
        return True

    def record(self):
        return {"schema": "missionos.starship_final_numerical_task_owner.v1", "kind": self.kind,
            "events": list(self.events), "cancelled": self.cancelled,
            "maximum_concurrent_children": 2 if self.kind == "forecast" else 1,
            "automatic_retry": False, "credential_free": True}


def worker(folder, token):
    from .starship_restored_outer import offline_worker
    from .starship_restored_environment import worker_environment_view
    shared.require(os.environ.get(TOKEN) == token, "final_numerical_worker_opt_in")
    actual = worker_environment_view()
    shared.require(set(actual) <= ENV_KEYS | {TOKEN, "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "PYTHONDONTWRITEBYTECODE"},
        "final_numerical_worker_unexpected_environment")
    folder = Path(folder)
    shared.require(folder.is_dir() and not folder.is_symlink()
        and sha256((folder/"job.json").read_bytes()).hexdigest() == token, "final_numerical_input_hash")
    job = shared.read(folder/"job.json", maximum_bytes=MAXIMUM_BYTES)
    shared.require(set(job) == {"schema", "kind", "payload"}
        and job["schema"] == "missionos.starship_final_numerical_job.v1", "final_numerical_job_shape")
    payload = job["payload"]
    ready = {"schema": "missionos.starship_final_numerical_process_ready.v1", "job_sha256": token,
        "pid": os.getpid(), "ppid": os.getppid(), "pgid": os.getpgrp()}
    shared.save(folder/"process-ready.json", ready)
    wait_until = time.monotonic()+5.
    while not (folder/"parent-observed.json").exists() and time.monotonic() < wait_until:
        time.sleep(.01)
    ack = shared.read(folder/"parent-observed.json", maximum_bytes=4096)
    shared.require(type(ack) is dict and set(ack) == {"schema", "job_sha256", "pid", "ppid", "pgid",
        "plan_sha256", "outer_host_pid", "observed_wall_s"}
        and ack["schema"] == "missionos.starship_final_numerical_parent_observation.v1"
        and all(ack[key] == ready[key] for key in ("job_sha256", "pid", "ppid", "pgid"))
        and type(ack["outer_host_pid"]) is int and ack["outer_host_pid"] > 1
        and type(ack["observed_wall_s"]) in (int, float) and ack["observed_wall_s"] <= time.monotonic(),
        "final_numerical_parent_process_observation_required")
    with offline_worker():
        if job["kind"] == "forecast":
            from .starship_final_sensor_prediction import forecast_job
            # forecast_job validates and owns its explicit source-bound policy;
            # this worker must not shadow it with a hardcoded/nested hook.
            result = forecast_job(payload)
        elif job["kind"] == "guard":
            from .starship_final_sensor_guard import check_bundle
            result = check_bundle(payload["bundle"], payload["authority"])
        else:
            raise ValueError("final_numerical_job_kind")
    value = {"schema": "missionos.starship_final_numerical_result.v1", "kind": job["kind"],
        "job_sha256": token, "checker_pid": os.getpid(), "credential_free": True, "result": result}
    with (folder/"result.json.gz").open("xb") as stream:
        stream.write(gzip.compress(shared.wire(value), mtime=0))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Owned final numerical child; never an unregistered mission")
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("token")
    args = parser.parse_args()
    worker(args.worker, args.token)
