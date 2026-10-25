"""Stop only an owned isolated worker and its two isolated forecast children.

The registered worker contract is deliberately narrow: at most two direct
new-session forecast children, no grandchildren, no alternate root waiter and
no new children after failure. The source/plan digests record the outer host's
binding; they do not authenticate arbitrary processes. No PID-only API exists.
No process name, argv, task content or provider data is collected.
"""
from hashlib import sha256
import json
import os
import re
import signal
import subprocess
import time

_POPEN_TYPE = subprocess.Popen
MAXIMUM_CHILDREN = 2
STOP_WAIT_S = 1.
TERMINATE_WAIT_S = .5
KILL_WAIT_S = 1.
REAP_WAIT_S = 2.


class RestoredProcessCleanupError(ValueError):
    """Rejected ownership is preserved without expanding any signal target."""

    def __init__(self, reason, receipt):
        super().__init__(reason)
        self.receipt = json.loads(json.dumps(receipt,sort_keys=True,allow_nan=False))


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _hash(value):
    return type(value) is str and re.fullmatch(r"[a-f0-9]{64}",value) is not None


def _table():
    """Trusted OS numeric fields only; never shell or process command strings."""
    output = subprocess.check_output(["/bin/ps","-axo","pid=,ppid=,pgid=,stat="],
        text=True,timeout=2.,env={"PATH":"/usr/bin:/bin","LC_ALL":"C"})
    result = {}
    for line in output.splitlines():
        fields = line.split()
        _require(len(fields) == 4 and all(re.fullmatch(r"[0-9]+",item) for item in fields[:3])
            and fields[3],"restored_process_owner_os_table_invalid")
        pid,ppid,pgid = map(int,fields[:3])
        _require(pid not in result,"restored_process_owner_os_table_duplicate_pid")
        result[pid] = {"pid":pid,"ppid":ppid,"pgid":pgid,"state":fields[3][0]}
    return result


def _root(table,pid,*,stopped=False):
    row = table.get(pid)
    _require(row is not None and row["ppid"] == os.getpid() and row["pgid"] == pid and pid > 1
        and row["state"] != "Z" and (not stopped or row["state"] in ("T","t")),
        "restored_process_owner_worker_identity_or_stop_lost")
    _require({item["pid"] for item in table.values() if item["pgid"] == pid} == {pid},
        "restored_process_owner_worker_group_not_exclusive")
    return row


def _child(table,pid,parent,*,stopped=False):
    row = table.get(pid)
    _require(row is not None and row["ppid"] == parent and row["pgid"] == pid and pid > 1
        and (not stopped or row["state"] in ("T","t","Z")),
        "restored_process_owner_child_identity_or_stop_lost")
    _require({item["pid"] for item in table.values() if item["pgid"] == pid} == {pid},
        "restored_process_owner_child_group_not_exclusive")
    return row


def _wait_until(predicate,seconds):
    until = time.monotonic()+seconds
    while True:
        table = _table()
        if predicate(table):
            return table
        _require(time.monotonic() < until,"restored_process_owner_stop_or_termination_timeout")
        time.sleep(.01)


def cleanup_restored_worker(worker, *, plan_sha256, source_sha256):
    """Consume a Popen owned by this host; freeze, terminate and verify cleanup.

    Never call concurrently with another waiter/cleanup for this Popen. Root
    SIGSTOP prevents spawning/reaping during ownership checks. Child groups die
    before root termination. The host can waitpid only its direct worker; dead
    grandchildren are finally reaped by the OS after root exit. No signals are
    sent to child IDs after the root has been reaped, when PID reuse is possible.
    An already-completed root is a no-signal noop, not proof of descendant exit.
    """
    _require(isinstance(worker,_POPEN_TYPE) and _hash(plan_sha256) and _hash(source_sha256)
        and type(worker.pid) is int and worker.pid > 1,"restored_process_owner_owned_popen_and_binding_required")
    receipt = {"schema":"missionos.starship_restored_process_cleanup.v1","plan_sha256":plan_sha256,
        "source_sha256":source_sha256,"host_pid":os.getpid(),"worker_pid":worker.pid,
        "maximum_direct_forecast_children":MAXIMUM_CHILDREN,"source_contract":"direct_new_session_children_only_no_grandchildren",
        "signals":[],"owned_children":[],"worker_reaped_by_host":False,"all_owned_processes_absent":False,
        "grandchildren_waitpid_performed_by_host":False,"no_child_signals_after_worker_reap":True,
        "model_calls":0,"plant_steps":0,"process_names_or_arguments_recorded":False}
    initial = worker.poll()
    if initial is not None:
        receipt.update(status="already_completed_no_signal",returncode=initial,worker_reaped_by_host=True,
            descendant_cleanup_verified=False)
        return receipt
    pid, frozen = worker.pid, []
    initial_table = _table()
    initial_row = initial_table.get(pid)
    if initial_row is not None and initial_row["ppid"] == os.getpid() and initial_row["pgid"] == pid and initial_row["state"] == "Z":
        receipt.update(status="completed_before_freeze_no_descendant_proof",returncode=worker.wait(timeout=KILL_WAIT_S),
            worker_reaped_by_host=True,descendant_cleanup_verified=False)
        return receipt
    _root(initial_table,pid)

    def send(target,kind,selected):
        if kind == "worker":
            os.kill(target,selected)
        else:
            os.killpg(target,selected)
        receipt["signals"].append({"kind":kind,"pid_or_pgid":target,"signal":signal.Signals(selected).name})

    def resume_live(target,kind):
        # Darwin can deliver TERM to a stopped process immediately. CONT on a
        # now-empty/zombie group may report EPERM; accept only OS-confirmed death.
        table = _table()
        row = table.get(target)
        if row is None or row["state"] == "Z":
            receipt.setdefault("dead_before_continue",[]).append(target)
            return
        if kind == "worker":
            _root(table,pid)
        else:
            _root(table,pid,stopped=True)
            _child(table,target,pid)
        try:
            send(target,kind,signal.SIGCONT)
        except (PermissionError,ProcessLookupError):
            after = _table().get(target)
            if after is not None and after["state"] != "Z":
                raise
            receipt.setdefault("dead_before_continue",[]).append(target)

    try:
        send(pid,"worker",signal.SIGSTOP)
        frozen.append((pid,"worker"))
        table = _wait_until(lambda current:current.get(pid,{}).get("state") in ("T","t","Z"),STOP_WAIT_S)
        if table[pid]["state"] == "Z":
            receipt.update(status="completed_during_freeze_no_descendant_proof",returncode=worker.wait(timeout=KILL_WAIT_S),
                worker_reaped_by_host=True,descendant_cleanup_verified=False)
            return receipt
        _root(table,pid,stopped=True)
        children = sorted(item["pid"] for item in table.values() if item["ppid"] == pid)
        _require(len(children) <= MAXIMUM_CHILDREN,"restored_process_owner_direct_child_contract_exceeded")
        for child in children:
            table = _table()
            _root(table,pid,stopped=True)
            row = _child(table,child,pid)
            receipt["owned_children"].append(dict(row))
            if row["state"] != "Z":
                send(child,"child_group",signal.SIGSTOP)
                frozen.append((child,"child_group"))
        table = _wait_until(lambda current:all(current.get(child,{}).get("state") in ("T","t","Z")
            for child in children),STOP_WAIT_S)
        _root(table,pid,stopped=True)
        for child in children:
            _child(table,child,pid,stopped=True)
        # A frozen complete tree is tiny by contract. Refuse unknown nested
        # sessions instead of expanding a kill target from an untrusted name.
        _require(not any(item["ppid"] in children for item in table.values()),
            "restored_process_owner_grandchildren_outside_registered_contract")
        for child in children:
            table = _table()
            _root(table,pid,stopped=True)
            row = _child(table,child,pid,stopped=True)
            if row["state"] != "Z":
                send(child,"child_group",signal.SIGTERM)
                resume_live(child,"child_group")
        until = time.monotonic()+TERMINATE_WAIT_S
        while time.monotonic() < until:
            table = _table()
            _root(table,pid,stopped=True)
            if all(child not in table or table[child]["state"] == "Z" for child in children):
                break
            time.sleep(.01)
        for child in children:
            table = _table()
            _root(table,pid,stopped=True)
            if child in table and table[child]["state"] != "Z":
                _child(table,child,pid)
                send(child,"child_group",signal.SIGKILL)
        table = _wait_until(lambda current:all(child not in current or current[child]["state"] == "Z"
            for child in children),KILL_WAIT_S)
        _root(table,pid,stopped=True)
        receipt["children_dead_before_worker_termination"] = True
        send(pid,"worker",signal.SIGTERM)
        resume_live(pid,"worker")
        try:
            result = worker.wait(timeout=KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            table = _table()
            _root(table,pid)
            send(pid,"worker",signal.SIGKILL)
            result = worker.wait(timeout=KILL_WAIT_S)
        # From this point onward no child PID/group is ever signaled again.
        frozen.clear()
        receipt.update(worker_reaped_by_host=True,returncode=result)
        until = time.monotonic()+REAP_WAIT_S
        while True:
            table = _table()
            absent = pid not in table and all(child not in table and not any(
                row["pgid"] == child for row in table.values()) for child in children)
            if absent or time.monotonic() >= until:
                break
            time.sleep(.01)
        receipt.update(status="cleaned" if absent else "unverified_os_reaping",all_owned_processes_absent=absent,
            descendant_cleanup_verified=absent,remaining_owned_ids=[item for item in [pid,*children] if item in table])
        return json.loads(json.dumps(receipt,sort_keys=True,allow_nan=False))
    except BaseException as error:
        # On a contract mismatch leave no stopped process behind. Resume only
        # identities still proven to be the originally owned direct children.
        try:
            table = _table()
        except (OSError,ValueError,subprocess.TimeoutExpired):
            table = {}
            receipt["resume_ownership_unavailable"] = True
        root = table.get(pid)
        if root is not None and root["ppid"] == os.getpid() and root["pgid"] == pid:
            for child,kind in reversed(frozen):
                row = table.get(child)
                if row is not None and row["state"] in ("T","t") and (kind == "worker" or
                        row["ppid"] == pid and row["pgid"] == child):
                    try:
                        send(child,kind,signal.SIGCONT)
                    except OSError as resume_error:
                        receipt.setdefault("resume_failures",[]).append({"kind":kind,"pid_or_pgid":child,
                            "error_type":type(resume_error).__name__})
        receipt.update(status="ownership_or_cleanup_rejected",descendant_cleanup_verified=False,
            error_type=type(error).__name__,reason=str(error) if isinstance(error,ValueError) else "cleanup_os_failure")
        if isinstance(error,Exception):
            raise RestoredProcessCleanupError(receipt["reason"],receipt) from error
        raise


def cleanup_receipt_sha256(receipt):
    return sha256(json.dumps(receipt,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
