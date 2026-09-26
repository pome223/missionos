"""Transport envelopes and real subprocess boundary; fixture inference only."""

import hashlib
import io
import json
import sys
import tarfile

import pytest

from scripts.run_urban_anwm_preview import (
    input_archive,
    receive_forecasts,
    stream_command,
    stream_forecasts,
)


def tar_payload(items):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, value in items:
            info = tarfile.TarInfo(name)
            if value is None:
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                archive.addfile(info)
            else:
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
    return buffer.getvalue()


def test_upload_never_includes_adjacent_secrets(tmp_path):
    for name in ("request.json", "assets.npz", ".dispatch-key", "approval.json"):
        (tmp_path / name).write_bytes(name.encode())
    with tarfile.open(fileobj=io.BytesIO(input_archive(tmp_path))) as archive:
        assert archive.getnames() == ["request.json", "assets.npz"]


@pytest.mark.parametrize(
    "member,value",
    [
        ("../escape", b"bad"),
        ("/absolute", b"bad"),
        ("result.json", None),
        ("unexpected.png", b"bad"),
    ],
)
def test_bad_result_archive_writes_nothing(tmp_path, member, value):
    out = tmp_path / "forecast"
    with pytest.raises(ValueError, match="archive"):
        receive_forecasts(tar_payload([(member, value)]), out, ["forward", "climb"])
    assert not out.exists()


def test_missing_or_duplicate_assets_are_rejected(tmp_path):
    for items in [[("result.json", b"{}")], [("result.json", b"{}"), ("result.json", b"{}")]]:
        with pytest.raises(ValueError, match="archive"):
            receive_forecasts(tar_payload(items), tmp_path / "forecast", ["forward", "climb"])
    assert not (tmp_path / "forecast").exists()


@pytest.mark.parametrize("identifier", ["../../other", "x;touch_bad", "x$(pwd)"])
def test_candidate_cannot_inject_remote_command(identifier):
    with pytest.raises(ValueError, match="bounded"):
        stream_command("/home/fixture/aerial-wam", "a" * 32, "b" * 64, ["forward", identifier])


def test_one_stream_executes_digest_checked_runtime_and_returns_exact_assets(tmp_path):
    remote = tmp_path / "remote"
    (remote / "venv/bin").mkdir(parents=True)
    (remote / "venv/bin/python").symlink_to(sys.executable)
    runtime = remote / "aerial_anwm_runtime.py"
    runtime.write_text("""import argparse,json
p=argparse.ArgumentParser();p.add_argument('--request');p.add_argument('--output-dir');a=p.parse_args()
request=json.loads(Path(a.request).read_text());out=Path(a.output_dir)
assert Path(a.request).with_name('assets.npz').read_bytes()==b'fixture input'
(out/'result.json').write_text(json.dumps({'fixture_invocation':True}))
for name in request['candidates']:
 for suffix in ['.png','-projection.png']:(out/(name+suffix)).write_bytes(b'fixture image')
print('runtime stdout must not corrupt the result archive')
""")
    bridge = tmp_path / "ssh-fixture.py"
    bridge.write_text(f"""import sys,subprocess
command=sys.argv[sys.argv.index('--command')+1].replace('/home/fixture/aerial-wam',{str(remote)!r})
raise SystemExit(subprocess.run(['bash','-c',command]).returncode)
""")
    prepared = tmp_path / "input"
    prepared.mkdir()
    (prepared / "request.json").write_text(json.dumps({"candidates": ["forward", "climb"]}))
    (prepared / "assets.npz").write_bytes(b"fixture input")
    cfg = {
        "remote_root": "/home/fixture/aerial-wam",
        "published_runtime_sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
    }
    destination = tmp_path / "forecast"
    stream_forecasts(
        [sys.executable, str(bridge)], cfg, "a" * 32, prepared, destination, ["forward", "climb"]
    )
    assert len(list(destination.iterdir())) == 5
    assert json.loads((destination / "result.json").read_text()) == {"fixture_invocation": True}
    cfg["published_runtime_sha256"] = "b" * 64
    with pytest.raises(RuntimeError, match="transport failed"):
        stream_forecasts(
            [sys.executable, str(bridge)],
            cfg,
            "c" * 32,
            prepared,
            tmp_path / "bad",
            ["forward", "climb"],
        )
    assert not (remote / "incoming" / ("c" * 32)).exists()
