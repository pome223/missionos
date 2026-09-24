"""Verify the R2 report and its separate prospective registration; no execution."""

from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location(
    "original_headroom_report_verifier",
    ROOT.parent / "urban-headroom-px4-20260924/verify_report.py",
)
original = importlib.util.module_from_spec(spec)
spec.loader.exec_module(original)
require = original.require


def verify_data(data, registration, timing, bindings):
    result = original.verify_data(data)
    frozen_raw = (ROOT / "frozen-protocol.json").read_bytes()
    frozen = json.loads(frozen_raw)
    require(
        hashlib.sha256(frozen_raw).hexdigest() == registration["frozen_record_sha256"]
        and frozen["source_sha256"] == registration["runtime_source_sha256"]
        and frozen["protocol"] == data["protocol"]
        and frozen["frozen_at_unix_s"] == registration["frozen_at_unix_s"]
        and frozen["cases_executed_at_freeze"] == 0,
        "original frozen record differs",
    )
    require(
        registration["cohort_id"] == "urban-headroom-r2-20260924", "cohort identity"
    )
    require(
        data["frozen_record_sha256"] == registration["frozen_record_sha256"]
        and data["runtime_source_sha256"] == registration["runtime_source_sha256"]
        and data["protocol_sha256"] == registration["protocol_sha256"],
        "registration binding differs",
    )
    require(
        registration["cases_executed_at_registration"] == 0
        and registration["previous_outcomes_combined"] is False
        and registration["same_scene_parameters_as_previous_incomplete_cohort"] is True
        and registration["additional_model_calls_permitted"] == 0
        and registration["rented_gpu_instances_permitted"] == 0
        and registration["no_retry_or_case_replacement"] is True
        and registration["stop_on_any_case_or_measurement_failure"] is True,
        "registration scope differs",
    )
    ordered = registration["ordered_cases"]
    require(ordered == data["protocol"]["cases"], "registered case order")
    attempted = {r["family"] for r in data["runs"]} | {
        r["case"] for r in data["failures"]
    }
    require(attempted == set(ordered[: len(attempted)]), "case skipped or replaced")
    require(
        timing["cohort_id"] == registration["cohort_id"]
        and timing["registration_preceded_first_case"] is True
        and timing["registration_url"]
        == "https://github.com/pome223/missionos/pull/113#issuecomment-5815010766"
        and timing["frozen_at_unix_s"] == registration["frozen_at_unix_s"]
        and registration["frozen_at_unix_s"]
        < datetime.fromisoformat(
            timing["github_created_at"].replace("Z", "+00:00")
        ).timestamp()
        < timing["driver_started_at_unix_s"],
        "registration did not precede execution",
    )
    require(
        [b["case"] for b in bindings] == ordered[: len(attempted)], "case binding order"
    )
    runs = {r["family"]: r for r in data["runs"]}
    previous = timing["driver_started_at_unix_s"]
    for binding in bindings:
        require(
            binding["case_started_at_unix_s"] >= previous,
            "case predates registration or order",
        )
        previous = binding["case_started_at_unix_s"]
        if binding["case"] not in runs:
            continue
        run = runs[binding["case"]]
        control = binding["probe_receipt"]
        require(
            binding["scene_ready_after_zero_probe_exit"] is True
            and control["subscriptions_released"] is True
            and control["probe_removed_observed"] is True
            and control["probe_contacts"] > 0
            and control["aircraft_commands_sent"] is False
            and control["model_invoked"] is False,
            "repaired preflight control missing",
        )
        require(
            hashlib.sha256((json.dumps(control, indent=2) + "\n").encode()).hexdigest()
            == binding["probe_receipt_sha256"]
            == run["depth_selection"]["source_sha256"]["contact-positive-control.json"]
            and binding["flight_result_sha256"]
            == run["source_sha256"]["flight-result.json"],
            "case receipt binding differs",
        )
    return {
        **result,
        "cohort_id": registration["cohort_id"],
        "registration_binding": "passed",
    }


def verify_observations(data, observations):
    runs = {r["family"]: r for r in data["runs"]}
    expected = {
        family
        for family in ("gap", "climb", "detour")
        if f"headroom_{family}_0" in runs
    }
    require(
        len(observations) == len(expected)
        and {r["family"] for r in observations} == expected,
        "illustration cases differ",
    )
    for row in observations:
        case = f"headroom_{row['family']}_0"
        require(
            row["case"] == case
            and row["file"] == row["family"] + "-observed.png"
            and row["generated_image"] is False
            and row["model_invoked"] is False
            and row["source_frames_sha256"]
            == runs[case]["source_sha256"]["route-images/frames.json"]
            and hashlib.sha256((ROOT / row["file"]).read_bytes()).hexdigest()
            == row["image_sha256"],
            "observed image binding differs",
        )


def verify():
    for name, expected in json.loads((ROOT / "manifest.json").read_text()).items():
        require(
            Path(name).name == name
            and hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected,
            "artifact hash mismatch",
        )
    data = json.loads((ROOT / "summary.json").read_text())
    verify_observations(data, json.loads((ROOT / "observations.json").read_text()))
    return verify_data(
        *[
            json.loads((ROOT / name).read_text())
            for name in (
                "summary.json",
                "registration.json",
                "registration-timing.json",
                "case-bindings.json",
            )
        ]
    )


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
