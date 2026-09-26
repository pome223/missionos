"""Resident queue lifecycle and real subprocess transport; model is a fixture."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from scripts import aerial_anwm_runtime as runtime
from scripts.run_urban_anwm_preview import stream_forecasts


def test_preloaded_identity_cannot_change_between_requests(tmp_path):
    loaded = {
        "upstream": tmp_path / "upstream",
        "checkpoint": tmp_path / "model",
        "diffusion_steps": 250,
    }
    request = {"upstream_root": "upstream", "checkpoint_path": "model"}
    runtime.bind_loaded_runtime(loaded, request, tmp_path, {"diffusion_steps": 250})
    for field, value in (
        ("upstream", tmp_path / "elsewhere"),
        ("checkpoint", tmp_path / "other"),
        ("diffusion_steps", 100),
    ):
        changed = {**loaded, field: value}
        with pytest.raises(ValueError, match="preloaded"):
            runtime.bind_loaded_runtime(changed, request, tmp_path, {"diffusion_steps": 250})


def test_worker_startup_failure_never_claims_ready(tmp_path, monkeypatch):
    def failed(*args):
        raise ValueError("wrong checkpoint")

    monkeypatch.setattr(runtime, "load_runtime", failed)
    with pytest.raises(ValueError, match="checkpoint"):
        runtime.resident_worker(tmp_path, tmp_path, tmp_path)
    assert not (tmp_path / "resident/ready.json").exists()
    assert json.loads((tmp_path / "resident/stopped.json").read_text())["handled_job_ids"] == []
    with pytest.raises(FileExistsError):
        runtime.resident_worker(tmp_path, tmp_path, tmp_path)


@pytest.mark.parametrize("count,seconds", [(0, 10), (4, 10), (1, 0), (1, 2401)])
def test_worker_enforces_operator_lifetime_bounds(tmp_path, count, seconds):
    with pytest.raises(ValueError, match="bound"):
        runtime.resident_worker(tmp_path / "worker", tmp_path, tmp_path, count, seconds)
    assert not (tmp_path / "worker").exists()


def test_invalid_queue_job_fails_without_inference_and_is_not_retried(tmp_path, monkeypatch):
    job = tmp_path / "incoming" / ("a" * 32)
    job.mkdir(parents=True)
    (job / ".ready").touch()
    (job / "request.json").symlink_to(tmp_path / "private.json")
    monkeypatch.setattr(
        runtime,
        "load_runtime",
        lambda *a: {
            "checkpoint_hash": "fixture",
            "initialization_seconds": 0,
            "load_seconds": 0,
        },
    )

    def forbidden(*args, **kwargs):
        pytest.fail("invalid queue must not invoke model")

    monkeypatch.setattr(runtime, "run", forbidden)
    runtime.resident_worker(tmp_path, tmp_path, tmp_path, max_requests=1, max_seconds=2)
    error = json.loads((tmp_path / "resident" / (job.name + "-error.json")).read_text())
    assert error["error_type"] == "ValueError"
    assert not (tmp_path / "output" / job.name).exists()
    assert not (tmp_path / "resident/ready.json").exists()


def test_resident_stream_process_reuses_one_load_for_two_fresh_requests(tmp_path):
    remote = tmp_path / "remote"
    (remote / "venv/bin").mkdir(parents=True)
    (remote / "venv/bin/python").symlink_to(sys.executable)
    wrapper = remote / "aerial_anwm_runtime.py"
    wrapper.write_text(f"""import sys,json
from pathlib import Path
sys.path.insert(0,{str(Path(__file__).resolve().parents[2])!r})
from scripts import aerial_anwm_runtime as m
m.__file__=__file__
root=Path(__file__).parent
def load(*args):
 with (root/'load-count').open('a') as f:f.write('1')
 return {{'checkpoint_hash':'fixture', 'initialization_seconds':0, 'load_seconds':0}}
def run(request,base,out,**kw):
 assert (root/'resident/ready.json').exists()
 assert (base/'assets.npz').read_bytes()==b'fixture input'
 out.mkdir()
 for name in request['candidates']:
  for suffix in ['.png','-projection.png']:(out/(name+suffix)).write_bytes(b'fixture image')
 return {{'fixture_invocation':True,'worker_id':kw['loaded']['worker_id'], 'request_id':request['request_id']}}
m.load_runtime=load;m.run=run
m.resident_worker(root,root,root,max_requests=2,max_seconds=20)
""")
    bridge = tmp_path / "ssh-fixture.py"
    bridge.write_text(f"""import sys,subprocess
command=sys.argv[sys.argv.index('--command')+1].replace('/home/fixture/aerial-wam',{str(remote)!r})
raise SystemExit(subprocess.run(['bash','-c',command]).returncode)
""")
    worker = subprocess.Popen(
        [sys.executable, str(wrapper)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        deadline = time.monotonic() + 5
        while not (remote / "resident/ready.json").exists():
            assert time.monotonic() < deadline and worker.poll() is None
            time.sleep(0.02)
        cfg = {
            "remote_root": "/home/fixture/aerial-wam",
            "transport": "resident_ssh_tar_v1",
            "published_runtime_sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest(),
        }
        results = []
        for identifier in ("a" * 32, "b" * 32):
            prepared = tmp_path / identifier
            prepared.mkdir()
            (prepared / "request.json").write_text(
                json.dumps(
                    {
                        "source_kind": runtime.URBAN_SOURCE,
                        "assets_npz": "assets.npz",
                        "request_id": identifier,
                        "candidates": ["forward", "climb"],
                    }
                )
            )
            (prepared / "assets.npz").write_bytes(b"fixture input")
            destination = tmp_path / (identifier + "-forecast")
            stream_forecasts(
                [sys.executable, str(bridge)],
                cfg,
                identifier,
                prepared,
                destination,
                ["forward", "climb"],
            )
            results.append(json.loads((destination / "result.json").read_text()))
        _, error = worker.communicate(timeout=5)
        assert worker.returncode == 0, error.decode()
        assert results[0]["worker_id"] == results[1]["worker_id"]
        assert results[0]["request_id"] != results[1]["request_id"]
        assert all(r["fixture_invocation"] for r in results)
        assert (remote / "load-count").read_text() == "1"
        assert not (remote / "resident/ready.json").exists()
        stopped = json.loads((remote / "resident/stopped.json").read_text())
        assert stopped["handled_job_ids"] == ["a" * 32, "b" * 32]
    finally:
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=5)
