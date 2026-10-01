"""Mock-only real production mailbox/transport boundary, no credential loader."""
import concurrent.futures
import json
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from src.intelligence.yokohama_jev_live import (
    BUDGET_ID, MAX_BYTES, MAX_REQUEST_USD, MODEL, BoundedTransport, LiveLedger,
    NoRedirect,
)
from src.intelligence.yokohama_pad_jev import configuration, judge
from src.runtime.task_store import TaskStore
from src.runtime.yokohama_payload import digest

HELPERS = runpy.run_path(str(Path(__file__).with_name("test_yokohama_jev.py")))

def initialize(path):
    # Test-only fixture registration. Production contains NO initializer.
    import os
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    marker = path.with_suffix(".initialized")
    descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE grant_record (budget_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO grant_record VALUES (?)", (BUDGET_ID,))
        conn.execute("CREATE TABLE delivery (task_id TEXT PRIMARY KEY, plan_sha256 TEXT NOT NULL)")
        conn.execute("CREATE TABLE sends (slot INTEGER PRIMARY KEY, request_id TEXT UNIQUE, "
                     "observation_id TEXT UNIQUE, payload_sha256 TEXT, maximum_usd REAL, reserved_at REAL)")


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "independent-budget.sqlite3"
    initialize(path)
    ledger = LiveLedger(path)
    ledger.claim("task", "plan")
    return ledger


def grant(monkeypatch):
    import src.intelligence.yokohama_jev_live as live
    monkeypatch.setattr(live, "LIVE_ENABLED", True)  # injected provider tests only
    monkeypatch.setenv("MISSIONOS_YOKOHAMA_JEV_MODE", "live")
    monkeypatch.setenv("MISSIONOS_YOKOHAMA_JEV_BUDGET_ID", BUDGET_ID)
    monkeypatch.setenv("TYPESAFE_API_KEY", "contract-fake-never-network")


class Opener:
    def __init__(self, fault=None, choice="continue"):
        self.calls = 0
        self.fault, self.choice = fault, choice

    def open(self, req, timeout):
        self.calls += 1
        assert req.full_url == "https://api.typesafe.ai/v1/systemone"
        assert len(req.data) <= MAX_BYTES
        assert 0 < timeout <= 15
        if self.fault == "timeout":
            raise TimeoutError("mock")
        payload = json.loads(req.data)
        raw = HELPERS["provider"](self.choice)(payload)
        raw["model"] = MODEL
        if self.fault == "invalid":
            raw["answers"]["response"]["probabilities"] = {"continue": .8, "hold": .8}
        if self.fault == "model":
            raw["model"] = "unexpected-version"
        self.body = json.dumps(raw).encode()
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n):
        return self.body


def transport(ledger, req=None, opener=None, deadline=None):
    return BoundedTransport(ledger=ledger, task_id="task", plan_sha256="plan",
                            request=req or HELPERS["request"](),
                            admission={"host_deadline_monotonic_s": deadline or time.monotonic()+20},
                            opener=opener or Opener())


@pytest.mark.parametrize("choice,action", [("continue", "enter"), ("hold", "wait")])
def test_live_current_zero_remaining_no_dispatch(monkeypatch, ledger, choice, action):
    grant(monkeypatch)
    t = transport(ledger, opener=Opener(choice=choice))
    a = judge(HELPERS["request"](), configuration(), transport=t)
    assert a["judge_status"] == "valid"
    assert a["decision"]["action"] == action
    assert a["decision"]["wait_seconds"] == (5 if choice == "hold" else 0)
    assert a["invocation"]["external_api_calls"] == 1
    assert a["invocation"]["inference_invoked"] is True
    assert a["invocation"]["reserved_slot"] == 1
    assert a["invocation"]["rationale_source"] == "adapter_template"
    assert "contract-fake" not in json.dumps(a)


@pytest.mark.parametrize("fault", ["timeout", "invalid", "model"])
def test_failed_send_consumes_and_reports_slot(monkeypatch, ledger, fault):
    grant(monkeypatch)
    t = transport(ledger, opener=Opener(fault))
    a = judge(HELPERS["request"](), configuration(), transport=t)
    assert a["judge_status"] == "unavailable"
    assert a["invocation"]["external_api_calls"] == 1
    assert a["invocation"]["reserved_slot"] == 1
    assert "decision" not in a
    with pytest.raises(ValueError, match="Replayed"):
        ledger.reserve("task", "plan", "fixture", "pad_1", "hash")


@pytest.mark.parametrize("fault", ["credential", "cap", "stale", "rules"])
def test_no_send_before_validation(monkeypatch, ledger, fault):
    grant(monkeypatch)
    req = HELPERS["request"]()
    if fault == "credential":
        monkeypatch.delenv("TYPESAFE_API_KEY")
    if fault == "cap":
        req["situation"]["extra"] = "x" * 32768
    if fault == "rules":
        req["situation"]["rules_action"] = "wait"
    t = transport(ledger, req, deadline=time.monotonic()-1 if fault == "stale" else None)
    a = judge(req, configuration(), transport=t)
    assert a["judge_status"] == "unavailable"
    assert t.opener.calls == 0
    assert t.slot is None


def test_atomic_parallel_restart_replay_cross_task_no_reset(ledger):
    def reserve(i):
        try:
            return ledger.reserve("task", "plan", str(i), "obs" + str(i), "hash")
        except ValueError:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        slots = list(pool.map(reserve, range(8)))
    assert sorted(x for x in slots if x) == [1, 2]
    reopened = LiveLedger(ledger.path)
    with pytest.raises(ValueError):
        reopened.claim("new-task", "new-plan")
    with pytest.raises(ValueError):
        reopened.reserve("new-task", "new-plan", "new", "new", "hash")
    with pytest.raises(ValueError):
        reopened.reserve("task", "plan", "third", "new", "hash")
    with pytest.raises(FileExistsError):
        initialize(ledger.path)
    assert 2 * MAX_REQUEST_USD < .01


def test_crash_after_reserve_keeps_consumption(ledger):
    script = ("from src.intelligence.yokohama_jev_live import LiveLedger; import os;"
              f"s=LiveLedger({str(ledger.path)!r});"
              "s.reserve('task','plan','crash','obs','hash'); os._exit(7)")
    result = subprocess.run([sys.executable, "-c", script], timeout=10)
    assert result.returncode == 7
    with pytest.raises(ValueError, match="Replayed"):
        ledger.reserve("task", "plan", "crash", "new-obs", "hash")
    with pytest.raises(ValueError, match="Replayed"):
        ledger.reserve("task", "plan", "new-id", "obs", "hash")
    assert ledger.reserve("task", "plan", "second", "second-obs", "hash") == 2


def test_redirect_never_followed_and_missing_ledger_never_created(tmp_path):
    assert NoRedirect().redirect_request(None, None, 302, "", {}, "https://other") is None
    missing = tmp_path / "missing.db"
    with pytest.raises(ValueError, match="never recreate"):
        LiveLedger(missing).connect()
    assert not missing.exists()


def test_production_worker_mailbox_live_host_smoke(monkeypatch, tmp_path):
    grant(monkeypatch)
    path, req, config, pad, gate = HELPERS["make_mailbox"](tmp_path)
    ledger_path = tmp_path / "ledger.sqlite3"
    initialize(ledger_path)
    ledger = LiveLedger(ledger_path)
    store = TaskStore(str(tmp_path / "tasks.db"))
    plan = {"mission_judge": configuration(), "goal": [10, 0], "route": [[0, 0], [10, 0]]}
    sha = digest(plan)
    config["map_plan_sha256"] = sha
    # Recreate the true worker-generated requests after config binding changes.
    import shutil
    shutil.rmtree(tmp_path / "pad-judge")
    from src.runtime.yokohama_pad_queue import make_request, propose, MissionJudgeGate
    observation = runpy.run_path(str(Path(__file__).with_name("test_yokohama_pad_queue.py")))["observation"]
    pad = make_request(config, 0, [observation(i) for i in range(6)])
    (tmp_path / "pad-decisions/000/request.json").write_text(json.dumps(pad))
    (tmp_path / "config.json").write_text(json.dumps(config))
    gate = MissionJudgeGate(tmp_path, config)
    gate.respond(pad, propose(config, pad))
    task = store.create(kind="test", title="live mock",
                        artifacts={"plan": plan, "plan_sha256": sha,
                                   "approval": {"plan_sha256": sha}})
    ledger.claim(task["task_id"], sha)
    import src.intelligence.yokohama_jev_live as live
    monkeypatch.setattr(live, "LiveLedger", lambda: ledger)
    opener = Opener()
    monkeypatch.setattr(live, "build_opener", lambda *args: opener)
    from src.gateway.yokohama_delivery_chat import YokohamaChatService
    YokohamaChatService._judge_pending(SimpleNamespace(store=store), task["task_id"], tmp_path,
                                      {"agents": plan["mission_judge"]}, set())
    answer = json.loads((path.parent / "response.json").read_text())
    assert answer["judge_status"] == "valid"
    assert opener.calls == 1
    assert answer["invocation"]["reserved_slot"] == 1
    assert answer["admission"]["clock_domain"] == "owned_worker_elapsed_monotonic"
    (path.parent / "response.json").unlink()
    YokohamaChatService._judge_pending(SimpleNamespace(store=store), task["task_id"], tmp_path,
                                      {"agents": plan["mission_judge"]}, set())
    assert opener.calls == 1  # Replay, even with new judged set after restart.
    assert json.loads((path.parent / "response.json").read_text())["judge_status"] == "unavailable"
    with sqlite3.connect(ledger_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

def test_child_configuration_roundtrip_without_credentials(monkeypatch, tmp_path):
    grant(monkeypatch)
    import os
    from src.runtime.yokohama_goal import GoalPlanner, approved_map_plan
    planner = GoalPlanner()
    plan = planner.plan(planner.legacy["delivery_pad"]["center_xyz_m"][:2])
    path = tmp_path / "approved.json"
    path.write_text(json.dumps({"plan": plan, "approval": {"plan_sha256": digest(plan),
                         "scene_version": plan["scene_version"], "approval_ref": "test"}}))
    env = os.environ.copy()
    for k in ("TYPESAFE_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        env.pop(k, None)
    result = subprocess.run([sys.executable, "-c",
                "import src.intelligence.yokohama_jev_live as live; live.LIVE_ENABLED=True; "
                "from src.runtime.yokohama_goal import approved_map_plan; "
                + f"assert approved_map_plan({str(path)!r})['mission_judge']['mode']=='live'"],
                env=env, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode()
    monkeypatch.setenv("MISSIONOS_YOKOHAMA_JEV_MODE", "fixture")
    with pytest.raises(ValueError):
        approved_map_plan(path)


def test_transport_same_instance_never_second_send(monkeypatch, ledger):
    grant(monkeypatch)
    t = transport(ledger)
    req = HELPERS["request"]()
    assert judge(req, configuration(), transport=t)["judge_status"] == "valid"
    assert judge(req, configuration(), transport=t)["judge_status"] == "unavailable"
    assert t.opener.calls == 1


def test_configuration_does_not_revive_old_budget(monkeypatch):
    grant(monkeypatch)
    monkeypatch.setenv("MISSIONOS_YOKOHAMA_JEV_MODE", "live")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("MISSIONOS_YOKOHAMA_JEV_BUDGET_ID", "old-consumed-budget")
    with pytest.raises(ValueError, match="closed"):
        configuration()
    monkeypatch.setenv("MISSIONOS_YOKOHAMA_JEV_BUDGET_ID", BUDGET_ID)
    assert configuration()["live_enabled"] is True  # setting, never API success


def test_missing_ledger_runtime_and_initializer_fail_closed(tmp_path):
    path = tmp_path / "lost.db"
    initialize(path)
    ledger = LiveLedger(path)
    ledger.claim("old", "plan")
    path.unlink()
    with pytest.raises(FileExistsError):
        initialize(path)
    with pytest.raises(ValueError, match="never recreate"):
        ledger.claim("new", "plan")

def test_grant_path_is_shared_across_worktrees_and_environment(tmp_path):
    import os
    from src.intelligence.yokohama_jev_live import LEDGER
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "alternate-home")
    env["MISSIONOS_STATE_ROOT"] = str(tmp_path / "alternate-state")
    # Absolute PYTHONPATH, so cwd can vary without using original checkout.
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    for cwd in (tmp_path, tmp_path / "other-worktree"):
        cwd.mkdir(exist_ok=True)
        result = subprocess.run([sys.executable, "-c",
            "from src.intelligence.yokohama_jev_live import LEDGER; print(LEDGER)"],
            cwd=cwd, env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0
        assert result.stdout.strip() == str(LEDGER)
    assert not LEDGER.is_relative_to(Path(__file__).resolve().parents[1])


def test_missing_entire_grant_fails_closed_no_runtime_initializer(tmp_path):
    import src.intelligence.yokohama_jev_live as live
    path = tmp_path / "entire-lost" / "budget.sqlite3"
    initialize(path)
    ledger = LiveLedger(path)
    ledger.claim("consumed", "plan")
    path.unlink()
    path.with_suffix(".initialized").unlink()
    path.parent.rmdir()
    with pytest.raises(ValueError, match="never recreate"):
        ledger.connect()
    assert not path.exists()
    assert not hasattr(live, "initialize")
    script = Path(__file__).resolve().parents[1] / "scripts/run_yokohama_jev_live_gateway.py"
    result = subprocess.run([sys.executable, str(script), "--initialize-budget"],
                            capture_output=True, timeout=10)
    assert result.returncode == 2  # Removed command, no implicit registration.


def test_other_store_cannot_claim_same_shared_grant(ledger):
    other_checkout_view = LiveLedger(ledger.path)
    with pytest.raises(ValueError, match="already consumed"):
        other_checkout_view.claim("new-task-in-other-checkout", "different-plan")
    assert other_checkout_view.reserve("task", "plan", "first", "obs", "hash") == 1
    assert ledger.reserve("task", "plan", "second", "obs2", "hash") == 2
    with pytest.raises(ValueError, match="exhausted"):
        other_checkout_view.reserve("task", "plan", "third", "obs3", "hash")


def test_public_live_blocked_before_credential_loader_and_http(monkeypatch, tmp_path):
    import src.intelligence.yokohama_jev_live as live
    assert live.LIVE_ENABLED is False
    monkeypatch.setenv("MISSIONOS_YOKOHAMA_JEV_MODE", "live")
    monkeypatch.setenv("MISSIONOS_YOKOHAMA_JEV_BUDGET_ID", BUDGET_ID)
    with pytest.raises(ValueError, match="closed"):
        configuration()
    script = Path(__file__).resolve().parents[1] / "scripts/run_yokohama_jev_live_gateway.py"
    invoked = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: invoked.append(a))
    monkeypatch.setattr(sys, "argv", [str(script), "--secret-project", "fixture-project"])
    with pytest.raises(SystemExit, match="no credential loader"):
        runpy.run_path(str(script), run_name="__main__")
    assert not invoked
    t = transport(LiveLedger(tmp_path / "missing.db"))
    with pytest.raises(ValueError, match="fixture only"):
        t({"model": MODEL})
    assert t.calls == 0 and t.opener.calls == 0 and t.slot is None
