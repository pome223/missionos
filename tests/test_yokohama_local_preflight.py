"""No real Docker/GCP calls; exercise ownership, freshness and paid admission."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts import yokohama_local_preflight as local
from scripts import yokohama_native_trial as trial
from test_yokohama_native_trial import approval, plan  # noqa: F401


@pytest.fixture
def fake(tmp_path, monkeypatch):
    preparation = local.Preparation(tmp_path / "probe", "sha256:" + "a" * 64, env={"PATH": "fixture"})
    state = SimpleNamespace(mode="success", allocated=False, calls=[], clock=10.0,
                            endpoint="unix:///fixture/docker.sock", daemon="fixture-daemon")
    cid = "b" * 64

    def command(args, timeout=local.API_TIMEOUT_S):
        state.calls.append(args)
        data = ""
        if args[:2] == ["context", "show"]:
            data = "fixture-context"
        elif args[:2] == ["context", "inspect"]:
            data = json.dumps(state.endpoint)
        elif args[0] == "info":
            data = state.daemon if args[-1] == "{{.ID}}" else json.dumps(
                dict(id=state.daemon, cpus=8, memory=8 * 1024**3))
        elif args[:2] == ["image", "inspect"]:
            data = preparation.image
        elif args[0] == "ps":
            data = cid if state.allocated else ""
        elif args[0] == "create":
            receipt = json.loads((preparation.root / "local-preparation.json").read_text())
            assert receipt["create_submitted"] is True and receipt["binding"]
            state.allocated = state.mode != "unknown_create"
            if state.mode in ("unknown_create", "lost_create_response"):
                raise subprocess.TimeoutExpired(args, timeout)
            data = cid
        elif args[0] == "start":
            assert timeout == 30
            if state.mode in ("start_timeout", "cleanup_error", "ownership_mismatch"):
                raise subprocess.TimeoutExpired(args, timeout)
            if state.mode == "interrupt":
                raise KeyboardInterrupt("fixture interruption")
            for name in local.MODEL_FILES:
                path = preparation.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture-model")
            (preparation.root / "local-preparation-output.txt").write_text(
                "wrong" if state.mode == "bad_nonce" else preparation.nonce)
            (preparation.root / "local-preparation-sha256.txt").write_text("\n".join(
                local.sha(preparation.root / name) + "  " + name for name in local.MODEL_FILES))
            if state.mode == "missing_model":
                (preparation.root / local.MODEL_FILES[0]).unlink()
        elif args[0] == "inspect":
            data = json.dumps(dict(id=cid, name="/" + preparation.name, image=preparation.image,
                labels={"missionos-local-preflight": "other" if state.mode == "ownership_mismatch" else preparation.nonce},
                mounts=[dict(Source=str(preparation.root), Destination="/mission")]))
        elif args[0] == "rm":
            assert args == ["rm", "--force", cid]
            if state.mode == "cleanup_error":
                raise subprocess.TimeoutExpired(args, timeout)
            state.allocated = False
        else:
            raise AssertionError(args)
        return SimpleNamespace(stdout=data, returncode=0)

    monkeypatch.setattr(preparation, "command", command)
    monkeypatch.setattr(local.time, "monotonic", lambda: state.clock)
    return preparation, state


def test_preparation_proves_copy_handshake_and_owned_cleanup(fake):
    preparation, state = fake
    assert preparation.run() is preparation
    preparation.assert_fresh()
    assert preparation.receipt["status"] == "passed" and not state.allocated
    assert preparation.receipt["cleanup_confirmed"] is True
    args = next(args for args in state.calls if args[0] == "create")
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--pull") + 1] == "never"
    assert "--gpus" not in args
    assert len(preparation.receipt["model_sha256"]) == len(local.MODEL_FILES)
    before = len(state.calls)
    with pytest.raises(ValueError, match="replayed"):
        preparation.run()
    assert len(state.calls) == before


@pytest.mark.parametrize("mode", ["start_timeout", "lost_create_response", "unknown_create",
    "bad_nonce", "missing_model", "cleanup_error", "ownership_mismatch", "interrupt"])
def test_failure_never_passes_and_cleanup_preserves_original_error(fake, mode):
    preparation, state = fake
    state.mode = mode
    with pytest.raises((Exception, KeyboardInterrupt)):
        preparation.run()
    assert preparation.receipt["status"] == "failed"
    assert preparation.completed_monotonic is None
    if mode in ("unknown_create", "cleanup_error", "ownership_mismatch"):
        assert preparation.receipt["cleanup_confirmed"] is False
    else:
        assert preparation.receipt["cleanup_confirmed"] is True and not state.allocated
    if mode == "ownership_mismatch":
        assert not any(args[0] == "rm" for args in state.calls)
    with pytest.raises(ValueError, match="live successful"):
        preparation.assert_fresh()


@pytest.mark.parametrize("age", [-1, 30.01, float("nan"), float("inf")])
def test_receipt_age_is_bounded_and_cannot_survive_process_restart(fake, age):
    preparation, state = fake
    preparation.run()
    state.clock += age
    with pytest.raises(ValueError, match="stale"):
        preparation.assert_fresh()
    reconstructed = local.Preparation(preparation.root, preparation.image, env=preparation.environment)
    with pytest.raises(ValueError, match="live successful"):
        reconstructed.assert_fresh()


@pytest.mark.parametrize("fault", ["daemon", "endpoint", "parent", "model"])
def test_changed_binding_blocks_admission(fake, fault):
    preparation, state = fake
    preparation.run()
    if fault == "daemon":
        state.daemon = "other"
    elif fault == "endpoint":
        preparation.endpoint = "unix:///other.sock"
    elif fault == "parent":
        preparation.binding["parent"]["inode"] += 1
    else:
        (preparation.root / local.MODEL_FILES[0]).write_text("changed")
    with pytest.raises(ValueError, match="stale"):
        preparation.assert_fresh()


def test_remote_docker_endpoint_is_rejected_before_container_creation(fake):
    preparation, state = fake
    state.endpoint = "tcp://remote.invalid:2375"
    with pytest.raises(ValueError, match="local Unix"):
        preparation.run()
    assert not any(args[0] == "create" for args in state.calls)
    assert preparation.receipt["cleanup_confirmed"] is True


def test_local_preflight_failure_prevents_even_cloud_preflight(plan, tmp_path, fake, monkeypatch):  # noqa: F811
    preparation, state = fake
    state.mode = "start_timeout"
    controller = trial.Trial(plan, tmp_path)
    monkeypatch.setattr(controller, "qualify_local", preparation.run)
    monkeypatch.setattr(controller, "preflight", lambda: pytest.fail("no cloud query before local prerequisites"))
    monkeypatch.setattr(controller, "call", lambda *a, **k: pytest.fail("no cloud create"))
    with pytest.raises(subprocess.TimeoutExpired):
        controller.execute(approval(plan))
    assert not controller.attempt.exists()
    assert preparation.receipt["cleanup_confirmed"] is True and not state.allocated


def test_expired_local_preflight_after_cloud_reads_still_prevents_create(plan, tmp_path, fake, monkeypatch):  # noqa: F811
    preparation, state = fake
    controller = trial.Trial(plan, tmp_path)

    def qualify():
        controller.local_preparation = preparation.run()

    def cloud_preflight():
        state.clock += 31
        return {"fixture": True}

    monkeypatch.setattr(controller, "qualify_local", qualify)
    monkeypatch.setattr(controller, "preflight", cloud_preflight)
    monkeypatch.setattr(controller, "call", lambda *a, **k: pytest.fail("stale check must not create"))
    cleaned = []
    monkeypatch.setattr(controller, "cleanup", lambda: cleaned.append(True))
    assert controller.execute(approval(plan)) == 1
    assert not controller.create_submitted and cleaned == [True]
    assert "stale" in (tmp_path / "controller-result.json").read_text()


def test_docker_environment_is_captured_not_reread_from_process(fake, monkeypatch):
    preparation, _ = fake
    monkeypatch.setenv("DOCKER_HOST", "tcp://unapproved.invalid:2375")
    preparation.run().assert_fresh()
    assert preparation.environment == {"PATH": "fixture", "DOCKER_HOST": "unix:///fixture/docker.sock"}


def test_context_default_changes_cannot_redirect_verified_endpoint(fake):
    preparation, state = fake
    preparation.run()
    state.endpoint = "tcp://changed-context.invalid:2375"
    state.calls.clear()
    preparation.assert_fresh()
    assert preparation.environment["DOCKER_HOST"] == "unix:///fixture/docker.sock"
    assert "DOCKER_CONTEXT" not in preparation.environment
    assert not any(args[0] == "context" for args in state.calls)


def test_slow_hash_reads_count_toward_freshness(fake, monkeypatch):
    preparation, state = fake
    preparation.run()
    original = local.sha

    def slow(path):
        state.clock += 31
        return original(path)

    monkeypatch.setattr(local, "sha", slow)
    with pytest.raises(ValueError, match="stale"):
        preparation.assert_fresh()


def test_changed_daemon_cannot_confirm_cleanup_from_empty_inventory(fake):
    preparation, state = fake
    preparation.run()
    state.daemon = "different-daemon"
    preparation.receipt["cleanup_confirmed"] = False
    with pytest.raises(RuntimeError, match="daemon changed"):
        preparation.cleanup()
    assert preparation.receipt["cleanup_confirmed"] is False


def test_slow_reservation_flush_prevents_cloud_submission(plan, tmp_path, fake, monkeypatch):  # noqa: F811
    preparation, state = fake
    controller = trial.Trial(plan, tmp_path)

    def qualify():
        controller.local_preparation = preparation.run()

    original = trial.write

    def slow(path, value):
        if path == controller.attempt and value.get("create_submitted") is True:
            state.clock += 31
        original(path, value)

    monkeypatch.setattr(controller, "qualify_local", qualify)
    monkeypatch.setattr(controller, "preflight", lambda: {"fixture": True})
    monkeypatch.setattr(trial, "write", slow)
    monkeypatch.setattr(controller, "call", lambda *a, **k: pytest.fail("no cloud create after slow fsync"))
    monkeypatch.setattr(controller, "cleanup", lambda: None)
    assert controller.execute(approval(plan)) == 1
    assert not controller.create_submitted
    assert json.loads(controller.attempt.read_text())["create_submitted"] is False
