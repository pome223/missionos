"""Study harness checks use serialized runtime evidence, not Python tuples."""
import json
from pathlib import Path

from scripts import study_starship_attitude_return as study


def test_short_real_run_is_verified_after_loading_saved_json(tmp_path, monkeypatch):
    original_simulate = study.simulate
    monkeypatch.setattr(study, "simulate", lambda profile, **kwargs: original_simulate(profile, duration_s=.2, **kwargs))
    profile = json.loads(Path("examples/spaceflight/starship-sixdof-profile.json").read_text())
    result = study._case((profile, 13, "mass_state_terminal_v3", str(tmp_path), {}))
    assert result["trajectory_verified"]
    assert result["return_record_verified"]
    assert result["termination"] == "time_limit"
    assert result["contact_point_speed_mps"] is None
    assert result["physical_execution"] is False
