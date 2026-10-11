import json
import time
from types import SimpleNamespace
import pytest
from scripts import yokohama_offline_vla_remote as remote
from scripts import ship_aerovla_server as server


def test_deadline_rejects_expired():
    with pytest.raises(TimeoutError):
        remote.deadline_check(time.time() - 1)


@pytest.mark.parametrize("fail_first", [False, True])
def test_fixed_pair_no_retry(monkeypatch, tmp_path, fail_first):
    from PIL import Image
    from scripts.yokohama_offline_vla_pair import PROMPT

    source = tmp_path / "inputs"
    for index in (1, 2):
        case = source / f"case-{index:02d}"
        case.mkdir(parents=True)
        (case / "prompt.txt").write_text(PROMPT)
        for key in ("rgb", "down"):
            Image.new("RGB", (640, 360), color=(index, 0, 0)).save(case / f"{key}.png")
    calls = []

    class Model:
        def __init__(self, *args):
            self.grammar = None
            self.cpu_between_requests = False
            self.load_seconds = 0
            self.torch = SimpleNamespace(
                __version__="2.9.1+cu129",
                cuda=SimpleNamespace(get_device_name=lambda n: "NVIDIA L4 CPU_TEST_FIXTURE"),
            )

        def predict(self, prompt, images):
            calls.append((prompt, [i.size for i in images]))
            if fail_first and len(calls) == 2:
                raise RuntimeError("CPU fixture error, no native inference")
            return {
                "generated_text": "0 49 49</s>",
                "generated_token_ids": [1],
            }, b"CPU_TEST_FIXTURE"

    monkeypatch.setattr(server, "NativeModel", Model)
    args = (tmp_path, tmp_path, source, tmp_path / "out", time.time() + 100, time.time() + 150)
    if fail_first:
        with pytest.raises(RuntimeError):
            remote.run(*args)
        assert len(calls) == 2
        assert not (tmp_path / "out/raw-response-02.json").exists()
    else:
        remote.run(*args)
        assert len(calls) == 3
        assert len({c[0] for c in calls}) == 1
        result = json.loads((tmp_path / "out/result.json").read_text())
        assert not result["raw_text_differs"]
        assert [r["case_index"] for r in result["receipts"]] == [1, 2]
        for i in (1, 2):
            req = json.loads((tmp_path / f"out/request-{i:02d}.json").read_text())
            assert set(req) == {"prompt", "images_base64"}
    counts = json.loads((tmp_path / "out/counts.json").read_text())
    assert counts["comparison_started"] == (1 if fail_first else 2)
