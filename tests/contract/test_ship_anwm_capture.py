"""The RGBD startup interval is fixed, retained and outside model history."""

import json
import threading
from types import SimpleNamespace as NS

from scripts.ship_anwm_capture import NativeHistory


def test_fixed_startup_bundles_do_not_create_gaps_or_replace_model_history(tmp_path):
    recorder = NativeHistory.__new__(NativeHistory)
    recorder.root = tmp_path
    recorder.config = dict(run_id="capture", world_sha256="world", plan_sha256="plan")
    recorder.lock = threading.Lock()
    recorder.active = True
    recorder.bundles = {}
    recorder.frames = []
    recorder.warmup_frames = []
    recorder.last_complete_stamp_ns = -1
    recorder.error = None
    for i in range(30):
        stamp = (i + 1) * 250_000_000
        header = NS(stamp=NS(sec=stamp // 10**9, nsec=stamp % 10**9))
        common = dict(header=header, SerializeToString=lambda: b"explicit-transport-double")
        # Different contents have no effect on the fixed exclusion count.
        rgb = NS(
            **common,
            width=640,
            height=360,
            step=1920,
            pixel_format_type=3,
            data=bytes([i]) * 691200,
        )
        depth = NS(
            **common, width=640, height=360, step=2560, pixel_format_type=13, data=b"\0" * 921600
        )
        info = NS(**common, width=640, height=360, intrinsics=NS(k=list(range(9))))
        vehicle = NS(
            name="x500_0",
            header=header,
            position=NS(x=0, y=0, z=30),
            orientation=NS(w=1, x=0, y=0, z=0),
        )
        poses = NS(**common, pose=[vehicle])
        for kind, message in (
            ("rgb", rgb),
            ("depth", depth),
            ("info", info),
            ("down", rgb),
            ("pose", poses),
        ):
            recorder.receive(kind, message)
    recorder.finish(None, lambda *args, **kwargs: None)
    capture = json.loads((tmp_path / "capture.json").read_text())
    warmup = capture["startup_warmup"]["frames"]
    frames = capture["frames"]
    assert len(warmup) == 8 and len(frames) == 20
    assert [r["simulation_time_ns"] for r in frames] == [i * 250_000_000 for i in range(9, 29)]
    assert capture["history_indices"] == list(range(16))
    assert capture["outcome_indices"] == list(range(16, 20))
    assert (tmp_path / warmup[0]["assets"]["rgb"]["file"]).read_bytes()[0] == 0
    assert (tmp_path / frames[0]["assets"]["rgb"]["file"]).read_bytes()[0] == 8
    assert (tmp_path / frames[-1]["assets"]["rgb"]["file"]).read_bytes()[0] == 27
    assert recorder.error is None
