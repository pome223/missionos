"""Trusted final host, one owned worker, nine inherited channels.

Models and operator authentication remain on this side of the process split.
The child retains actual plant ownership and numerical copied tasks. The outer
record independently observes process IDs; it is consistency evidence, never
an OS attestation or proof of a model's unrecorded internal reasoning.
"""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import os
import secrets
import shutil
import subprocess
import time

from . import starship_final_shared as shared
from .starship_final_timing import MissionWallBudget
from .starship_final_plan import verification_inputs, validate_plan
from .starship_final_protocol import FinalObservationHost
from .starship_final_visual import FinalVisualHost, FinalVisualPolicy
from .starship_final_fixture import FinalFixtureTransport, restored_agent_factory
from .starship_final_gateway import scripted_operator
from .starship_inflight_runtime import approval_pipe, ApprovalPipeWriter, ApprovalPipeReader
from .starship_restored_native_host import RestoredNativeHost
from .starship_restored_integration import RestoredIntegrationPolicy
from .starship_restored_process_owner import cleanup_restored_worker, RestoredProcessCleanupError, _table
from src.intelligence.starship_final_supervision_agent import FinalSupervisionAgent
from src.intelligence.starship_final_sensor_agent import FinalSensorAgent

TOKEN = "MISSIONOS_STARSHIP_FINAL_WORKER_PLAN"
CHANNEL_NAMES = ("decision_request_writer", "decision_response_reader", "ready_writer", "ready_reader",
    "approval_request_writer", "approval_grant_reader", "telemetry_writer", "observation_request_writer", "observation_response_reader")
WRITE_INDICES = frozenset({0, 2, 4, 6, 7})


@contextmanager
def worker_channels(fds):
    shared.require(type(fds) is list and len(fds) == 9 and len(set(fds)) == 9
        and all(type(fd) is int and fd > 2 for fd in fds), "final_worker_nine_distinct_fds")
    channels = []
    try:
        for index, fd in enumerate(fds):
            channels.append((ApprovalPipeWriter if index in WRITE_INDICES else ApprovalPipeReader)(fd))
        yield dict(zip(CHANNEL_NAMES, channels))
    finally:
        for channel in channels:
            channel.close()


def child_environment(plan_hash):
    return {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"}} | {
        TOKEN: plan_hash, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def host_wrapper(channel, record, inputs):
    return {"schema": "missionos.starship_final_host_evidence.v1", "channel": channel,
        "plan_sha256": inputs["plan_sha256"], "source_sha256": shared.digest(inputs["source_sha256"]),
        "context": deepcopy(inputs["context"]), "mode": inputs["mode"], "record": record}


def run_final_child(plan, *, folder, worker_command, service, address, fixture_visual_endpoint=None,
                    clock=time.monotonic, sleep=time.sleep, cohort_budget=None, wall_clock=None):
    """Caller has already consumed a concrete execution approval and run claim."""
    validate_plan(plan, current=True)
    inputs = verification_inputs(plan)
    mode, folder = inputs["mode"], Path(folder)
    shared.require(folder.is_dir() and not folder.is_symlink() and callable(worker_command), "final_outer_exclusive_folder")
    shared.require((mode == "fixture") == (fixture_visual_endpoint is not None), "final_outer_fixture_endpoint_scope")
    timing = MissionWallBudget(inputs["bounds"]["per_mission_wall_s"], clock=clock, wall_clock=wall_clock,
        reason="final_outer_wall_budget_stop_no_retry")
    (folder/"camera").mkdir(exist_ok=False)
    pairs = [approval_pipe() for _ in range(9)]
    fds = [pair[1 if index in WRITE_INDICES else 0].fd for index, pair in enumerate(pairs)]
    active, process, closed = [True], None, set()
    host = rpc = visual = supervision = sensor = None
    observations, transcript, hashes = {}, [], {}
    operator_step = scripted_operator(plan, address)

    def budgets():
        if cohort_budget is not None:
            cohort_budget()
        limits = inputs["bounds"]
        timing.check()
        shared.require(shutil.disk_usage(folder).free >= limits["minimum_free_bytes"]
            and sum(path.stat().st_size for path in folder.rglob("*") if path.is_file()) <= limits["maximum_output_bytes"],
            "final_outer_disk_budget_stop_no_retry")

    def authorized(request, provider):
        return active[0] is True and mode == "live" and provider in ("typesafe", "deepseek")

    def handler(request, *, remaining_s, outer_deadline_monotonic):
        if request["domain"] == "visual":
            return visual.assess(request["observation"])
        if request["domain"] == "sensor":
            return sensor.assess(request["observation"], remaining_s=remaining_s,
                outer_deadline_monotonic=outer_deadline_monotonic)
        result = supervision.assess(request, remaining_s=remaining_s, outer_deadline_monotonic=outer_deadline_monotonic)
        if result["status"] in ("valid", "fixture_valid") and result["action"] == "request_deep_reasoning":
            result = supervision.assess(request, provider="deepseek",
                remaining_s=max(0., outer_deadline_monotonic-clock()), outer_deadline_monotonic=outer_deadline_monotonic)
        return result

    def record_host():
        records = {"restored": None if host is None else host.record(),
            "supplemental": None if rpc is None else rpc.record(),
            "visual": None if visual is None else visual.record(), "sensor": None if sensor is None else sensor.record()}
        for channel, record in records.items():
            path = folder/("host-"+channel+".json")
            if record is not None and not path.exists():
                hashes[channel] = shared.save(path, host_wrapper(channel, record, inputs))

    try:
        host = RestoredNativeHost(context=inputs["context"], plan_sha256=inputs["plan_sha256"],
            expected_source_sha256=inputs["source_sha256"], profile_raw_bytes=shared.relative(shared.PROFILE).read_bytes(),
            integration_policy=RestoredIntegrationPolicy.from_record(inputs["integration_policy"]), mode=mode, governance_dir=folder/"governance",
            ledger_path=folder/"operator.sqlite", signing_key=secrets.token_bytes(32),
            operator_token=(token:=secrets.token_hex(24)), operator_id="final-scripted-simulation-principal",
            decision_request_reader=pairs[0][0], decision_response_writer=pairs[1][1], ready_reader=pairs[2][0], ack_writer=pairs[3][1],
            approval_request_reader=pairs[4][0], approval_grant_writer=pairs[5][1], telemetry_reader=pairs[6][0],
            service=service, agent_factory=restored_agent_factory if mode == "fixture" else None,
            active=lambda: active[0], clock=clock, environment=inputs["backend"],
            return_review_policy=inputs["return_review_policy"])
        transport = FinalFixtureTransport() if mode == "fixture" else None
        supervision = FinalSupervisionAgent(mode=mode, transport=transport, live_authorized=authorized,
            active=lambda: active[0], clock=clock)
        sensor = FinalSensorAgent(mode=mode, transport=FinalFixtureTransport() if mode == "fixture" else None,
            live_authorized=authorized, active=lambda: active[0], clock=clock)
        visual = FinalVisualHost(folder/"camera", run_id=plan["run_id"], plan_sha256=inputs["plan_sha256"],
            source_sha256=inputs["source_sha256"], mode=mode, policy=FinalVisualPolicy(**plan["configuration"]["visual_policy"]),
            fixture_endpoint=fixture_visual_endpoint, live_authorized=lambda: active[0] and mode == "live",
            active=lambda: active[0], clock=clock, ledger_path=folder/"visual-host-attempts.jsonl")
        rpc = FinalObservationHost(pairs[7][0], pairs[8][1], plan_sha256=inputs["plan_sha256"],
            source_sha256=shared.digest(inputs["source_sha256"]), handler=handler, active=lambda: active[0], clock=clock)
        command = worker_command(fds)
        shared.require(type(command) is list and command and all(type(word) is str for word in command), "final_worker_command")
        with (folder/"worker.log").open("xb") as stream:
            process = subprocess.Popen(command, cwd=shared.ROOT, env=child_environment(inputs["plan_sha256"]),
                pass_fds=fds, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        for index, pair in enumerate(pairs):
            endpoint = pair[1 if index in WRITE_INDICES else 0]
            endpoint.close()
            closed.add(endpoint.fd)
        probe = -1.
        while process.poll() is None:
            timing.check()
            if clock()-probe >= 1.:
                budgets()
                table = _table()
                for row in table.values():
                    if row["ppid"] == process.pid and row["pgid"] == row["pid"]:
                        if row["pid"] not in observations:
                            observations[row["pid"]] = {**row, "first_seen_wall_s": clock()}
                        observations[row["pid"]]["last_seen_wall_s"] = clock()
                # The child waits before any numerical work until the parent
                # has independently observed its PID/group. This closes the
                # fast saved-checker gap without claiming OS attestation or
                # renewing the task's original deadline.
                for ready_path in folder.glob("numerical-*/*/process-ready.json"):
                    ack_path = ready_path.with_name("parent-observed.json")
                    if ack_path.exists():
                        continue
                    ready = shared.read(ready_path, maximum_bytes=4096)
                    seen = observations.get(ready.get("pid"))
                    shared.require(type(ready) is dict and set(ready) == {"schema", "job_sha256", "pid", "ppid", "pgid"}
                        and ready["schema"] == "missionos.starship_final_numerical_process_ready.v1"
                        and ready_path.resolve().is_relative_to(folder.resolve()), "final_outer_numerical_ready_shape")
                    if seen is not None:
                        shared.require(ready["ppid"] == seen["ppid"] == process.pid
                            and ready["pgid"] == seen["pgid"] == ready["pid"], "final_outer_numerical_ready_pid_mismatch")
                        shared.save(ack_path, {**ready, "schema": "missionos.starship_final_numerical_parent_observation.v1",
                            "plan_sha256": inputs["plan_sha256"], "outer_host_pid": os.getpid(), "observed_wall_s": clock()})
                # Two operational forecasts + six sensor samples + two guards.
                # This new final composition does not relax legacy's two-child
                # contract or allow additional completed full flights.
                shared.require(len(observations) <= 10, "final_outer_child_census_exceeded")
                probe = clock()
            host.tick()
            rpc.tick()
            operator_step(host, token, transcript)
            sleep(.01)
        process.wait()
        budgets()  # A child that exits during sleep cannot bypass the deadline.
        # Drain already queued evidence once; never issue a replacement request.
        host.tick()
        rpc._collect_answers()
        shared.require(process.returncode == 0, "final_outer_worker_failed_no_retry")
        table = _table()
        shared.require(all(pid not in table and not any(row["pgid"] == pid for row in table.values())
            for pid in observations), "final_outer_numerical_groups_not_reaped")
        record_host()
        timing.finish()
        timing_hash = shared.save(folder/"outer-timing.json", {"component": "outer",
            "plan_sha256": inputs["plan_sha256"], "source_sha256": shared.digest(inputs["source_sha256"]),
            "timing": timing.record()})
        shared.save(folder/"outer-process.json", {"schema": "missionos.starship_final_outer_process.v2",
            "timing_sha256": timing_hash,
            "plan_sha256": inputs["plan_sha256"], "source_sha256": shared.digest(inputs["source_sha256"]),
            "worker_pid": process.pid, "worker_exit_code": process.returncode,
            "observed_children": list(observations.values()), "observed_child_groups_absent": True,
            "all_children_exhaustively_observed": False, "os_attestation": False})
        record_host()
        return {"host_sha256": hashes, "worker_exit_code": 0}
    finally:
        active[0] = False
        try:
            if process is not None and process.poll() is None:
                try:
                    cleanup = cleanup_restored_worker(process, plan_sha256=inputs["plan_sha256"],
                        source_sha256=shared.digest(inputs["source_sha256"]))
                except RestoredProcessCleanupError as error:
                    cleanup = error.receipt
                shared.save(folder/"abnormal-process-cleanup.json", cleanup)
            record_host()
            if not (folder/"operator-http.json").exists():
                shared.save(folder/"operator-http.json", transcript)
        finally:
            try:
                if not (folder/"outer-timing.json").exists():
                    shared.save(folder/"outer-timing.json", {"component": "outer",
                        "plan_sha256": inputs["plan_sha256"], "source_sha256": shared.digest(inputs["source_sha256"]),
                        "timing": timing.record()})
            finally:
                try:
                    if host is not None:
                        host.close()
                finally:
                    for pair in pairs:
                        for endpoint in pair:
                            if endpoint.fd not in closed:
                                endpoint.close()
