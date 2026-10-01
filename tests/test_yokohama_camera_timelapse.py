import hashlib
import json

import pytest

from scripts.export_yokohama_camera_timelapse import load_frames, prior_frame


def fixture_frames(tmp_path):
    (tmp_path / 'camera.png').write_bytes(b'fixture camera bytes')
    digest = hashlib.sha256(b'fixture camera bytes').hexdigest()
    rows = [{'image': {'file': 'camera.png', 'sha256': digest, 'sensor_sim_s': stamp}}
            for stamp in [1.0, 3.5]]
    (tmp_path / 'frames.json').write_text(json.dumps(rows))
    return rows


def test_export_prior_camera_never_future_or_stale(tmp_path):
    fixture_frames(tmp_path)
    rows = load_frames(tmp_path, 'frames.json')
    assert prior_frame(rows, .9) is None
    assert prior_frame(rows, 3.4) is None
    assert prior_frame(rows, 3.6) == rows[1]


@pytest.mark.parametrize('fault', ['hash', 'foreign_path', 'clock_backwards', 'clock_nan'])
def test_export_rejects_image_substitution_and_invalid_clock(tmp_path, fault):
    rows = fixture_frames(tmp_path)
    if fault == 'hash':
        rows[0]['image']['sha256'] = 'bad'
    if fault == 'foreign_path':
        rows[0]['image']['file'] = '../foreign.png'
    if fault == 'clock_backwards':
        rows[1]['image']['sensor_sim_s'] = .5
    if fault == 'clock_nan':
        rows[1]['image']['sensor_sim_s'] = float('nan')
    (tmp_path / 'frames.json').write_text(json.dumps(rows))
    with pytest.raises(ValueError):
        load_frames(tmp_path, 'frames.json')


def test_fixture_video_never_claims_real_provider_and_preserves_attribution(tmp_path, monkeypatch):
    from PIL import Image, ImageDraw
    import scripts.export_yokohama_camera_timelapse as exporter

    run = tmp_path / "fixture-run"
    run.mkdir()
    Image.new("RGB", (640, 360), "#404040").save(run / "camera.png")
    digest = hashlib.sha256((run / "camera.png").read_bytes()).hexdigest()
    rows = [{"image": {"file": "camera.png", "sha256": digest, "sensor_sim_s": stamp}}
            for stamp in [1., 3.5]]
    for name in ["video-frames.json", "queue-video-frames.json", "payload-video-frames.json"]:
        (run / name).write_text(json.dumps(rows))
    (run / "config.json").write_text(json.dumps({"mission_judge": {"mode": "fixture"}}))
    drawn = []
    original = ImageDraw.ImageDraw.text
    def capture(self, xy, text, *args, **kwargs):
        drawn.append(text)
        return original(self, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    def encode(cmd, **kwargs):
        from pathlib import Path
        Path(cmd[-1]).write_bytes(b"mock encoded video; never a flight or API")
    monkeypatch.setattr(exporter.subprocess, "run", encode)
    monkeypatch.setattr(exporter.subprocess, "check_output", lambda *args, **kwargs: json.dumps({
        "streams": [{"codec_name": "h264", "pix_fmt": "yuv420p", "width": 640,
                     "height": 480, "nb_frames": "6"}], "format": {"duration": "0.25"}}))
    receipt = exporter.export(run, tmp_path / "video")
    assert not any("Jev" in text or "real" in text or "CPU PX4" in text for text in drawn)
    assert any("provider/model use documented separately" in text for text in drawn)
    assert any("Yokohama City / Project PLATEAU" in text and "CC BY 4.0" in text
               and "MissionOS modifications" in text for text in drawn)
    assert any("https://www.geospatial.jp/ckan/dataset/" in text for text in drawn)
    assert receipt["attribution"]["details"].endswith("ATTRIBUTION.md")
    assert receipt["provider_claim"].startswith("none;")
