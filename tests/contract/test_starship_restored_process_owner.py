"""Mock OS ownership/signal contracts; no plant/model or real process kill."""
from copy import deepcopy
import os
import signal
import subprocess
from types import SimpleNamespace

import pytest

from src.runtime import starship_restored_process_owner as owner


@pytest.fixture
def system(monkeypatch):
    root, children, unrelated = 10000, [10001,10002], 20000
    processes = {pid:{"pid":pid,"ppid":os.getpid() if pid in (root,unrelated) else root,
        "pgid":pid,"state":"S"} for pid in [root,*children,unrelated]}
    calls, flags = [], {"ignore_child_term":False,"ignore_root_term":False,"reuse_after_reap":False}
    class Process:
        pid = root
        args = ["not-recorded-private-task-data"]
        returncode = None
        def poll(self):
            return self.returncode
        def wait(self,timeout):
            if flags["ignore_root_term"] and not any(target == root and selected == signal.SIGKILL for _,target,selected in calls):
                raise subprocess.TimeoutExpired("owned-fixture",timeout)
            self.returncode = -15
            processes.pop(root,None)
            for child in children:
                processes.pop(child,None)
            if flags["reuse_after_reap"]:
                processes[children[0]] = {"pid":children[0],"ppid":999,"pgid":children[0],"state":"S"}
            return self.returncode
    process = Process()
    def send(kind,target,selected):
        assert target in (root,*children),"no signal may target unrelated processes"
        calls.append((kind,target,selected))
        if selected == signal.SIGSTOP:
            processes[target]["state"] = "T"
        elif target in children and selected == signal.SIGTERM and not flags["ignore_child_term"]:
            processes[target]["state"] = "Z"
        elif selected == signal.SIGKILL:
            processes[target]["state"] = "Z" if target in children else "S"
        elif selected == signal.SIGCONT and processes[target]["state"] != "Z":
            processes[target]["state"] = "S"
    monkeypatch.setattr(owner,"_POPEN_TYPE",Process)
    monkeypatch.setattr(owner,"_table",lambda:deepcopy(processes))
    monkeypatch.setattr(owner.os,"kill",lambda target,selected:send("pid",target,selected))
    monkeypatch.setattr(owner.os,"killpg",lambda target,selected:send("group",target,selected))
    counter = iter(index*.05 for index in range(10000))
    monkeypatch.setattr(owner.time,"monotonic",lambda:next(counter))
    monkeypatch.setattr(owner.time,"sleep",lambda _delay:None)
    return SimpleNamespace(process=process,processes=processes,calls=calls,flags=flags,root=root,children=children,unrelated=unrelated)


def clean(value):
    return owner.cleanup_restored_worker(value.process,plan_sha256="a"*64,source_sha256="b"*64)


def test_stops_owned_root_then_child_groups_kills_children_before_reaping_root(system):
    result = clean(system)
    assert result["status"] == "cleaned" and result["all_owned_processes_absent"]
    assert result["worker_reaped_by_host"] and not result["grandchildren_waitpid_performed_by_host"]
    assert result["children_dead_before_worker_termination"]
    assert system.calls[0] == ("pid",system.root,signal.SIGSTOP)
    root_term = system.calls.index(("pid",system.root,signal.SIGTERM))
    assert all(system.calls.index(("group",child,signal.SIGTERM)) < root_term for child in system.children)
    assert system.unrelated in system.processes and all(target != system.unrelated for _,target,_ in system.calls)
    assert "not-recorded-private-task-data" not in str(result)
    assert len(owner.cleanup_receipt_sha256(result)) == 64


def test_child_and_root_ignoring_term_escalate_only_still_proven_owned_targets(system):
    system.flags.update(ignore_child_term=True,ignore_root_term=True)
    result = clean(system)
    assert result["all_owned_processes_absent"]
    assert all(("group",child,signal.SIGKILL) in system.calls for child in system.children)
    assert ("pid",system.root,signal.SIGKILL) in system.calls
    root_kill = system.calls.index(("pid",system.root,signal.SIGKILL))
    assert all(system.calls.index(("group",child,signal.SIGKILL)) < root_kill for child in system.children)


def test_completed_noop_sends_no_signals_and_does_not_claim_unknown_descendant_cleanup(system,monkeypatch):
    system.process.returncode = 0
    monkeypatch.setattr(owner,"_table",lambda:pytest.fail("completed noop must not enumerate or signal reused PID"))
    result = clean(system)
    assert result["status"] == "already_completed_no_signal" and result["signals"] == []
    assert result["worker_reaped_by_host"] and not result["descendant_cleanup_verified"]


@pytest.mark.parametrize("change",["not_child","not_new_session","unrelated_group_member"])
def test_root_ownership_refuses_without_any_signal(system,change):
    if change == "not_child":
        system.processes[system.root]["ppid"] = 999
    elif change == "not_new_session":
        system.processes[system.root]["pgid"] = 999
    else:
        system.processes[system.unrelated]["pgid"] = system.root
    with pytest.raises(ValueError):
        clean(system)
    assert system.calls == []


@pytest.mark.parametrize("change",["third_child","child_not_new_session","child_has_unrelated_group_member","grandchild"])
def test_changed_source_process_contract_resumes_frozen_identities_without_term_or_kill(system,change):
    if change == "third_child":
        system.processes[30000] = {"pid":30000,"ppid":system.root,"pgid":30000,"state":"S"}
    elif change == "child_not_new_session":
        system.processes[system.children[0]]["pgid"] = 999
    elif change == "child_has_unrelated_group_member":
        system.processes[system.unrelated]["pgid"] = system.children[0]
    else:
        system.processes[30000] = {"pid":30000,"ppid":system.children[0],"pgid":30000,"state":"S"}
    with pytest.raises(owner.RestoredProcessCleanupError) as error:
        clean(system)
    assert error.value.receipt["status"] == "ownership_or_cleanup_rejected"
    assert not error.value.receipt["descendant_cleanup_verified"]
    assert system.processes[system.root]["state"] == "S"
    assert all(selected not in (signal.SIGTERM,signal.SIGKILL) for _,_,selected in system.calls)
    assert all(target != system.unrelated for _,target,_ in system.calls)


def test_pid_reuse_after_root_reaping_never_causes_new_child_signal(system):
    system.flags["reuse_after_reap"] = True
    result = clean(system)
    assert result["status"] == "unverified_os_reaping" and not result["all_owned_processes_absent"]
    assert result["remaining_owned_ids"] == [system.children[0]]
    assert result["no_child_signals_after_worker_reap"]
    # All child signals precede the last root CONT; reused ID gets no signal.
    final_root = max(index for index,(_,target,_) in enumerate(system.calls) if target == system.root)
    assert all(index < final_root for index,(_,target,_) in enumerate(system.calls) if target in system.children)


@pytest.mark.parametrize("bad",[True,12345,{"pid":12345},None])
def test_pid_or_arbitrary_data_cannot_replace_owned_popen(bad):
    with pytest.raises(ValueError,match="owned_popen"):
        owner.cleanup_restored_worker(bad,plan_sha256="a"*64,source_sha256="b"*64)


def test_os_table_uses_absolute_ps_numeric_fields_only(monkeypatch):
    seen = []
    def ps(argv,**kwargs):
        seen.append((argv,kwargs))
        return " 10 2 10 S+\n11 10 11 T\n"
    monkeypatch.setattr(owner.subprocess,"check_output",ps)
    result = owner._table()
    assert result == {10:{"pid":10,"ppid":2,"pgid":10,"state":"S"},11:{"pid":11,"ppid":10,"pgid":11,"state":"T"}}
    assert seen[0][0] == ["/bin/ps","-axo","pid=,ppid=,pgid=,stat="]
    assert "shell" not in seen[0][1] and seen[0][1]["timeout"] == 2.


def test_natural_exit_between_poll_and_freeze_is_no_signal_no_descendant_claim(system):
    system.processes[system.root]["state"] = "Z"
    result = clean(system)
    assert result["status"] == "completed_before_freeze_no_descendant_proof"
    assert result["signals"] == [] and not result["descendant_cleanup_verified"]


def test_natural_exit_during_sigstop_is_reaped_without_descendant_signal(system,monkeypatch):
    original = owner.os.kill
    def send(pid,selected):
        original(pid,selected)
        if selected == signal.SIGSTOP:
            system.processes[pid]["state"] = "Z"
    monkeypatch.setattr(owner.os,"kill",send)
    result = clean(system)
    assert result["status"] == "completed_during_freeze_no_descendant_proof"
    assert all(kind == "pid" and selected == signal.SIGSTOP for kind,_,selected in system.calls)


def test_darwin_continue_eperm_is_accepted_only_after_confirming_child_dead(system,monkeypatch):
    system.flags["ignore_child_term"] = True
    original = owner.os.killpg
    def send(pid,selected):
        if selected == signal.SIGCONT:
            system.processes[pid]["state"] = "Z"
            raise PermissionError("synthetic Darwin empty group")
        original(pid,selected)
    monkeypatch.setattr(owner.os,"killpg",send)
    result = clean(system)
    assert result["all_owned_processes_absent"] and set(result["dead_before_continue"]) == set(system.children)


def test_live_continue_permission_failure_preserves_rejected_receipt_not_success(system,monkeypatch):
    system.flags["ignore_child_term"] = True
    original = owner.os.killpg
    def send(pid,selected):
        if selected == signal.SIGCONT:
            raise PermissionError("synthetic live permission denial")
        original(pid,selected)
    monkeypatch.setattr(owner.os,"killpg",send)
    with pytest.raises(owner.RestoredProcessCleanupError) as error:
        clean(system)
    assert not error.value.receipt["descendant_cleanup_verified"]
    assert error.value.receipt["resume_failures"]
