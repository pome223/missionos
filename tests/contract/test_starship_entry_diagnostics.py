"""Crossing coverage and fit provenance must not become a false accuracy claim."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.runtime.starship_entry_diagnostics import ENTRY_IDS, FIT_IDS, diagnose_entry


ROOT = Path(__file__).resolve().parents[2]


def fixture():
    heights = [70800, 41100, 12900, 2300, 1400, 800, 500, 200, 100]
    times = [100, 150, 200, 300, 310, 320, 330, 340, 350]
    offsets = [90, 70, 50, 50, 52, 48, 51, 49, 50]
    reference = {
        "source_url": "https://example.invalid/manual-readings",
        "altitude_basis": "unknown",
        "observations": [
            {"id": identity, "time_s": time, "altitude_m": height}
            for identity, time, height in zip(ENTRY_IDS, times, heights)
        ],
    }
    trace = [{"time_s": 0, "altitude_m": 120000}]
    trace.extend(
        {"time_s": time + offset, "altitude_m": height}
        for time, offset, height in zip(times, offsets, heights)
    )
    trace.append({"time_s": 420, "altitude_m": 0})
    run = {
        "scenario": "flight14_inspired",
        "traces": {"ship": trace},
        "events": [
            {"event": "entry_interface", "body": "ship", "time_s": 0},
            {"event": "surface_contact", "body": "ship", "time_s": 420},
        ],
    }
    return run, reference


def small(trace, entry=0):
    run = {
        "scenario": "fixture",
        "traces": {"ship": [{"time_s": t, "altitude_m": h} for t, h in trace]},
        "events": [{"event": "entry_interface", "body": "ship", "time_s": entry}],
    }
    reference = {"observations": [{"id": "V17", "time_s": 10, "altitude_m": 50}]}
    return run, reference


def test_offset_shape_and_provenance_are_separate_and_read_only():
    run, reference = fixture()
    original = deepcopy((run, reference))
    result = diagnose_entry(run, reference)
    assert (run, reference) == original
    assert result["covered_points"] == 9
    fit = result["lower_segment_alignment"]
    assert fit["fit_ids"] == list(FIT_IDS)
    assert fit["median_offset_s"] == 50
    assert fit["max_abs_fit_residual_s"] == 2
    assert fit["fit_delay_range_s"] == [48, 52]
    assert result["rows"][0]["residual_after_offset_s"] == 40
    assert not result["rows"][0]["alignment_fit_point"]
    assert all(r["alignment_fit_point"] for r in result["rows"][3:])
    segment = result["segments"][0]
    assert segment["observed_elapsed_s"] == 100
    assert segment["model_elapsed_s"] == 60
    assert segment["model_to_observed_duration_ratio"] == 0.6
    assert result["surface_contact"]["observed_splashdown_time_s"] is None
    assert result["surface_contact"]["model_events"] == [{"time_s": 420}]
    assert result["rows"][-1]["model_time_s"] == 400
    assert not result["accuracy_verified"] and not result["operational_cause_verified"]
    assert not result["trajectory_modified"]
    run["traces"]["ship"][1]["altitude_m"] += 1
    changed = diagnose_entry(run, reference)
    assert (
        changed["provenance"]["canonical_selected_input_sha256"]
        != result["provenance"]["canonical_selected_input_sha256"]
    )
    assert (
        changed["provenance"]["canonical_reference_sha256"]
        == result["provenance"]["canonical_reference_sha256"]
    )


def test_sparse_samples_preserve_bracket_and_do_not_claim_timing_accuracy():
    result = diagnose_entry(*small([(0, 100), (100, 0)]))
    row = result["rows"][0]
    assert row["model_time_s"] == 50 and row["delay_s"] == 40
    assert row["crossings"][0]["bracket_s"] == [0, 100]
    assert row["crossings"][0]["interpolation_fraction"] == 0.5
    assert row["crossings"][0]["bracket_width_s"] == 100
    assert result["trace_summary"]["maximum_sample_gap_s"] == 100
    assert not result["accuracy_verified"]


def test_time_shift_is_a_separate_bracketed_diagnostic_and_never_changes_trace():
    run, reference = fixture()
    # Exact common delay fixture: independently known shape and offset.
    for sample, observation in zip(run["traces"]["ship"][1:-1], reference["observations"]):
        sample["time_s"] = observation["time_s"] + 50
    original = deepcopy(run)
    result = diagnose_entry(run, reference)
    metrics = result["same_time_altitude_diagnostic"]
    assert metrics["offset_s"] == 50
    assert metrics["original"]["mean_absolute_difference_m"] > 0
    assert metrics["offset_aligned"]["mean_absolute_difference_m"] == 0
    assert metrics["offset_aligned"]["full_coverage"]
    assert metrics["offset_aligned"]["rows"][-1]["evaluation_time_s"] == 400
    assert metrics["offset_aligned"]["rows"][-1]["bracket_s"] == [390, 400]
    assert run == original
    assert not result["accuracy_verified"]


def test_shifted_time_outside_saved_trace_is_not_extrapolated_or_scored_as_zero():
    run, reference = fixture()
    run["traces"]["ship"] = run["traces"]["ship"][:-1]
    # A short early pass still covers V25, but the median alignment shifts its
    # comparison time beyond the final stored sample.
    run["traces"]["ship"][-1]["time_s"] = 399
    result = diagnose_entry(run, reference)
    assert result["lower_segment_alignment"]["full_coverage"]
    aligned = result["same_time_altitude_diagnostic"]["offset_aligned"]
    assert aligned["rows"][-1]["status"] == "outside_trace"
    assert aligned["rows"][-1]["model_altitude_m"] is None
    assert aligned["covered_points"] == 8
    assert not aligned["full_coverage"]
    assert aligned["mean_absolute_difference_m"] is None


@pytest.mark.parametrize(
    "trace", [[(0, 100), (5, 50)], [(0, 100), (5, 50), (10, 0)], [(0, 50), (5, 0)]]
)
def test_exact_endpoints_are_valid_and_not_double_counted(trace):
    row = diagnose_entry(*small(trace))["rows"][0]
    assert row["status"] == "covered"
    assert len(row["crossings"]) == 1


@pytest.mark.parametrize("trace", [[(0, 0), (10, 100)], [(0, 100), (10, 60)], [(0, 100)]])
def test_ascending_uncovered_or_single_sample_is_not_a_zero_error(trace):
    result = diagnose_entry(*small(trace))
    row = result["rows"][0]
    assert row["status"] == "missing_descending_crossing"
    assert row["model_time_s"] is row["delay_s"] is None
    assert result["covered_points"] == 0
    assert result["lower_segment_alignment"]["median_offset_s"] is None


def test_crossings_before_the_recorded_entry_event_are_excluded():
    result = diagnose_entry(*small([(0, 100), (10, 0), (20, 100), (30, 0)], entry=20))
    row = result["rows"][0]
    assert row["status"] == "covered" and row["model_time_s"] == 25
    assert row["crossings"][0]["sample_indices"] == [2, 3]


@pytest.mark.parametrize(
    "trace", [[(0, 100), (10, 0), (20, 100), (30, 0)], [(0, 100), (10, 50), (20, 50), (30, 0)]]
)
def test_multiple_descents_or_level_interval_are_explicitly_ambiguous(trace):
    row = diagnose_entry(*small(trace))["rows"][0]
    assert row["status"] == "ambiguous_crossing"
    assert row["model_time_s"] is row["delay_s"] is None
    assert len(row["crossings"]) >= 2


def test_unique_crossings_on_rebound_do_not_define_a_negative_elapsed_segment():
    run, reference = small([(0, 60000), (10, 10000), (20, 80000), (30, 50000)])
    reference["observations"] = [
        {"id": "V17", "time_s": 10, "altitude_m": 70800},
        {"id": "V19", "time_s": 20, "altitude_m": 12900},
    ]
    result = diagnose_entry(run, reference)
    assert result["covered_points"] == 2
    assert result["rows"][0]["model_time_s"] > result["rows"][2]["model_time_s"]
    segment = result["segments"][0]
    assert segment["status"] == "nonmonotonic_crossings"
    assert segment["endpoint_coverage"]
    assert segment["crossings_chronological"] is False
    assert segment["model_elapsed_s"] is None
    assert segment["elapsed_difference_s"] is None
    assert segment["model_to_observed_duration_ratio"] is None


def test_six_unique_fit_crossings_require_chronological_descent_order():
    run, reference = small([(0, 900), (10, 0), (20, 3000), (30, 1000)])
    reference["observations"] = [
        {"id": identity, "time_s": time, "altitude_m": height}
        for identity, time, height in zip(FIT_IDS, range(1, 7), [2300, 1400, 800, 500, 200, 100])
    ]
    result = diagnose_entry(run, reference)
    fit = result["lower_segment_alignment"]
    assert fit["fit_covered_points"] == 6
    assert fit["full_coverage"]
    assert fit["crossings_chronological"] is False
    assert not fit["alignment_available"]
    assert fit["status"] == "nonmonotonic_crossings"
    assert fit["median_offset_s"] is None
    assert fit["max_abs_fit_residual_s"] is None
    assert all(row["residual_after_offset_s"] is None for row in result["rows"])
    assert result["same_time_altitude_diagnostic"]["offset_aligned"]["covered_points"] == 0


@pytest.mark.parametrize("kind", ["missing", "duplicate", "outside"])
def test_no_implicit_entry_phase_selection(kind):
    run, reference = fixture()
    if kind == "missing":
        run["events"] = []
    elif kind == "duplicate":
        run["events"].append(deepcopy(run["events"][0]))
    else:
        run["events"][0]["time_s"] = 1000
    result = diagnose_entry(run, reference)
    assert result["covered_points"] == 0
    assert result["entry_interface"]["status"] != "available"
    assert result["lower_segment_alignment"]["median_offset_s"] is None


def test_one_missing_fit_reference_disables_the_entire_offset():
    run, reference = fixture()
    reference["observations"][-1]["id"] = "V99"
    result = diagnose_entry(run, reference)
    assert result["rows"][-1]["status"] == "missing_reference"
    assert result["covered_points"] == 8
    assert result["lower_segment_alignment"]["fit_covered_points"] == 5
    assert not result["lower_segment_alignment"]["full_coverage"]
    assert result["lower_segment_alignment"]["median_offset_s"] is None
    assert all(r["residual_after_offset_s"] is None for r in result["rows"])
    assert result["segments"][1]["model_elapsed_s"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("time_s", float("nan")),
        ("altitude_m", float("inf")),
        ("time_s", True),
        ("altitude_m", "50"),
    ],
)
@pytest.mark.parametrize("location", ["trace", "reference"])
def test_nonfinite_or_nonnumeric_selected_data_rejected(field, value, location):
    run, reference = fixture()
    selected = run["traces"]["ship"] if location == "trace" else reference["observations"]
    selected[1][field] = value
    with pytest.raises(ValueError, match="finite number"):
        diagnose_entry(run, reference)


@pytest.mark.parametrize(
    "mutation", ["duplicate_time", "reverse_time", "duplicate_reference", "reverse_reference"]
)
def test_order_or_identity_errors_are_not_silently_sorted(mutation):
    run, reference = fixture()
    if mutation == "duplicate_time":
        run["traces"]["ship"][1]["time_s"] = 0
    elif mutation == "reverse_time":
        run["traces"]["ship"].reverse()
    elif mutation == "duplicate_reference":
        reference["observations"].append(reference["observations"][0])
    else:
        reference["observations"][1]["time_s"] = 0
    with pytest.raises(ValueError):
        diagnose_entry(run, reference)


@pytest.mark.parametrize(
    "location,value",
    [
        ("run", []),
        ("reference", []),
        ("traces", []),
        ("ship", {}),
        ("sample", 1),
        ("events", {}),
        ("events", [1]),
        ("observations", {}),
        ("observations", [1]),
    ],
)
def test_malformed_structures_fail_explicitly(location, value):
    run, reference = fixture()
    if location == "run":
        run = value
    elif location == "reference":
        reference = value
    elif location == "ship":
        run["traces"]["ship"] = value
    elif location == "sample":
        run["traces"]["ship"][0] = value
    elif location == "observations":
        reference[location] = value
    else:
        run[location] = value
    with pytest.raises(ValueError):
        diagnose_entry(run, reference)


def test_cli_binds_input_bytes_and_preserves_saved_evidence_and_output(tmp_path):
    run, reference = fixture()
    source = tmp_path / "study.json"
    source.write_text(
        json.dumps(
            {"runs": [run], "public_reference": reference, "verification": {"study_verified": True}}
        )
    )
    before = source.read_bytes()
    output = tmp_path / "diagnostic.json"
    command = [
        sys.executable,
        str(ROOT / "scripts/analyze_starship_entry.py"),
        "--input-run",
        str(source),
        "--output",
        str(output),
    ]
    first = subprocess.run(command, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    result = json.loads(output.read_text())
    assert result["file_bindings"]["input_file_sha256"] == sha256(before).hexdigest()
    assert not result["file_bindings"]["input_verification_performed"]
    assert not result["provenance"]["input_authentication_performed"]
    assert result["provenance"]["stored_verification_is_historical_only"]
    assert source.read_bytes() == before
    written = output.read_bytes()
    second = subprocess.run(command, capture_output=True, text=True, check=False)
    assert second.returncode == 2
    assert output.read_bytes() == written
    assert source.read_bytes() == before


def test_cli_explicit_reference_binding_and_missing_scenario_failure(tmp_path):
    run, reference = fixture()
    source, ref, output = (
        tmp_path / name for name in ("run.json", "reference.json", "diagnostic.json")
    )
    source.write_text(json.dumps(run))
    ref.write_text(json.dumps(reference))
    command = [
        sys.executable,
        str(ROOT / "scripts/analyze_starship_entry.py"),
        "--input-run",
        str(source),
        "--reference",
        str(ref),
        "--output",
        str(output),
    ]
    failed = subprocess.run([*command, "--scenario", "absent"], capture_output=True, check=False)
    assert failed.returncode == 2 and not output.exists()
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    result = json.loads(output.read_text())
    assert result["file_bindings"]["reference_file_sha256"] == sha256(ref.read_bytes()).hexdigest()
