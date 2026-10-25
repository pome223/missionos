"""Mechanical parent lifecycle shared by separately registered native callers.

No fixture choices, objectives, provider permission, operator policy or physical
verification lives here. Callers explicitly supply their host mode and optional
operator step. Worker credentials are never inherited; host secrets stay here.
"""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import time
from unittest.mock import patch

from .starship_inflight_runtime import approval_pipe, ApprovalPipeReader, ApprovalPipeWriter
from .starship_restored_native_host import RestoredNativeHost
from .starship_restored_process_owner import cleanup_restored_worker, RestoredProcessCleanupError, _table


def require(value, reason):
    if not value:
        raise ValueError(reason)


def wire(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False)+"\n").encode()


def save(path, value):
    with Path(path).open("xb") as handle:
        handle.write(wire(value))


def child_environment(token_name, token):
    require(type(token_name) is str and re.fullmatch(r"MISSIONOS_[A-Z0-9_]+",token_name)
        and type(token) is str and re.fullmatch(r"[a-f0-9]{64}",token),"restored_outer_worker_token_invalid")
    return {key:value for key,value in os.environ.items() if key in {"PATH","HOME","TMPDIR","LANG","LC_ALL"}} | {
        token_name:token,"OPENBLAS_NUM_THREADS":"1","OMP_NUM_THREADS":"1"}


@contextmanager
def offline_worker():
    """Inherited pipes and numerical subprocesses only; no worker providers."""
    from src.intelligence.starship_m2_broker import DeadlineTransport
    def forbidden(*_args,**_kwargs):
        raise ValueError("restored_outer_worker_network_or_provider_forbidden")
    with patch.object(socket.socket,"connect",forbidden),patch.object(socket.socket,"connect_ex",forbidden), \
            patch.object(socket.socket,"sendto",forbidden),patch.object(socket,"create_connection",forbidden), \
            patch.object(socket,"getaddrinfo",forbidden),patch.object(socket,"gethostbyname",forbidden), \
            patch.object(DeadlineTransport,"_invoke",forbidden):
        yield


@contextmanager
def worker_channels(fds):
    require(type(fds) is list and len(fds) == 7 and len(set(fds)) == 7
        and all(type(fd) is int and fd > 2 for fd in fds),"restored_outer_seven_worker_fds_required")
    channels = []
    names = ("decision_request_writer","decision_response_reader","ready_writer","ready_reader",
        "approval_request_writer","approval_grant_reader","telemetry_writer")
    try:
        for index,fd in enumerate(fds):
            channels.append((ApprovalPipeWriter if index in (0,2,4,6) else ApprovalPipeReader)(fd))
        yield dict(zip(names,channels))
    finally:
        for channel in channels:
            channel.close()


def tracked_children(process, seen):
    for row in _table().values():
        if row["ppid"] == process.pid and row["pgid"] == row["pid"]:
            seen.add(row["pid"])
    require(len(seen) <= 2,"restored_outer_worker_child_contract_changed")


def no_outstanding_children(seen, *, normal_exit=True):
    until = time.monotonic()+2.
    while True:
        table = _table()
        missing = all(pid not in table and not any(row["pgid"] == pid for row in table.values()) for pid in seen)
        if missing or time.monotonic() >= until:
            break
        time.sleep(.01)
    require(missing,"restored_outer_forecast_children_not_reaped")
    return {"tracked_child_pids":sorted(seen),"all_tracked_children_and_groups_absent":True,
        "normal_source_bound_worker_finally_closed_forecasts":normal_exit,
        "descendant_cleanup_verified":normal_exit,"all_descendants_exhaustively_observed_by_host":False,
        "no_signal_sent_after_worker_reaped":True}


def run_native_child(*, folder, cwd, context, plan_sha256, source_sha256, profile_raw_bytes,
        integration_policy, mode, environment, service, worker_command, worker_token_name,
        operator_id, check_wall, check_resources, operator_step=None, agent_factory=None,
        resource_monitor_interval_s=1., host_poll_interval_s=.01, return_review_policy=None):
    """Run one owned child and retain originals; caller checks saved results.

    operator_step(host, token, transcript) is an explicit trusted-host callback.
    This helper never calls an operator or selects/approves a candidate itself.
    A live host always constructs its inherited production agent, never fixtures.
    """
    require(mode in ("fixture","live") and ((mode == "live" and agent_factory is None)
        or (mode == "fixture" and callable(agent_factory))),"restored_outer_provider_mode_required")
    require(callable(worker_command) and callable(check_wall) and callable(check_resources)
        and (operator_step is None or callable(operator_step))
        and type(resource_monitor_interval_s) in (int,float) and resource_monitor_interval_s == 1.
        and type(host_poll_interval_s) in (int,float) and host_poll_interval_s == .01,
        "restored_outer_registered_callbacks_or_cadence_required")
    require(type(source_sha256) is dict and source_sha256.get("src/runtime/starship_restored_outer.py")
        == sha256(Path(__file__).read_bytes()).hexdigest(),"restored_outer_source_pin_missing_or_changed")
    if return_review_policy is not None:
        from .starship_restored_return_review import validate_policy
        return_review_policy = validate_policy(return_review_policy)
        require(source_sha256.get("src/runtime/starship_restored_return_review.py")
            == sha256(Path(__file__).with_name("starship_restored_return_review.py").read_bytes()).hexdigest(),
            "restored_outer_return_review_source_opt_in_required")
    folder = Path(folder)
    pairs = [approval_pipe() for _ in range(7)]
    fds = [pair[1 if index in (0,2,4,6) else 0].fd for index,pair in enumerate(pairs)]
    alive,process,seen,normal_verified,closed_fds = [True],None,set(),False,set()
    host,transcript = None,[]
    try:
        host = RestoredNativeHost(context=context,plan_sha256=plan_sha256,expected_source_sha256=source_sha256,
            profile_raw_bytes=profile_raw_bytes,integration_policy=integration_policy,mode=mode,
            governance_dir=folder/"governance",ledger_path=folder/"operator.sqlite",signing_key=secrets.token_bytes(32),
            operator_token=(token:=secrets.token_hex(24)),operator_id=operator_id,
            decision_request_reader=pairs[0][0],decision_response_writer=pairs[1][1],ready_reader=pairs[2][0],ack_writer=pairs[3][1],
            approval_request_reader=pairs[4][0],approval_grant_writer=pairs[5][1],telemetry_reader=pairs[6][0],
            service=service,agent_factory=agent_factory,active=lambda:alive[0],environment=environment,
            **({"return_review_policy":return_review_policy} if return_review_policy is not None else {}))
        command = worker_command(fds)
        require(type(command) is list and command and all(type(word) is str for word in command),"restored_outer_worker_command_invalid")
        last_probe = 0.
        with (folder/"worker.log").open("xb") as log:
            process = subprocess.Popen(command,cwd=cwd,env=child_environment(worker_token_name,plan_sha256),pass_fds=fds,
                stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            for index,pair in enumerate(pairs):
                endpoint = pair[1 if index in (0,2,4,6) else 0]
                endpoint.close()
                closed_fds.add(endpoint.fd)
            while process.poll() is None:
                check_wall()
                if time.monotonic()-last_probe >= resource_monitor_interval_s:
                    check_resources()
                    tracked_children(process,seen)
                    last_probe = time.monotonic()
                host.tick()
                if operator_step is not None:
                    operator_step(host,token,transcript)
                time.sleep(host_poll_interval_s)
        alive[0] = False
        save(folder/"normal-process-closure.json",no_outstanding_children(seen,normal_exit=process.returncode == 0))
        normal_verified = True
        require(process.returncode == 0,"restored_outer_worker_failed_no_retry")
        host.tick()
        raw = wire(host.record())
        with (folder/"host-record.json").open("xb") as handle:
            handle.write(raw)
        save(folder/"operator-http.json",transcript)
        return {"host_record_sha256":sha256(raw).hexdigest(),"operator_http":transcript}
    finally:
        alive[0] = False
        try:
            if process is not None and not normal_verified:
                try:
                    receipt = cleanup_restored_worker(process,plan_sha256=plan_sha256,
                        source_sha256=sha256(json.dumps(source_sha256,sort_keys=True,separators=(",",":")).encode()).hexdigest())
                except RestoredProcessCleanupError as error:
                    save(folder/"process-cleanup.json",error.receipt)
                    raise
                save(folder/"process-cleanup.json",receipt)
                if receipt.get("descendant_cleanup_verified") is not True:
                    save(folder/"abnormal-process-closure.json",no_outstanding_children(seen,normal_exit=False))
                    require(receipt.get("status") in ("already_completed_no_signal","completed_before_freeze_no_descendant_proof",
                        "completed_during_freeze_no_descendant_proof"),"restored_outer_owned_cleanup_unverified")
        finally:
            try:
                if host is not None:
                    if not (folder/"host-record.json").exists():
                        save(folder/"host-record.json",host.record())
                    if not (folder/"operator-http.json").exists():
                        save(folder/"operator-http.json",transcript)
            finally:
                try:
                    if host is not None:
                        host.close()
                finally:
                    for pair in pairs:
                        for endpoint in pair:
                            if endpoint.fd not in closed_fds:
                                endpoint.close()
