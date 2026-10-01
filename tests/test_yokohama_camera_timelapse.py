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
