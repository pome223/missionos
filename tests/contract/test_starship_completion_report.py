"""Portable report numbers, claim boundaries and saved-point replay controls."""
from copy import deepcopy
from hashlib import sha256
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import shutil
import subprocess

import pytest

from src.runtime.starship_completion_report import build_completion_report


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.current = None
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.current = [dict(attrs), ""]

    def handle_data(self, value):
        if self.current is not None:
            self.current[1] += value

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.scripts.append(self.current)
            self.current = None


@pytest.fixture
def profile():
    source = Path(__file__).resolve().parents[2]/"examples"/"spaceflight"/"starship-sixdof-profile.json"
    result = json.loads(source.read_text())
    result["launch"].update(latitude_deg=0., longitude_deg=0.)
    return result


def sample(time, east, height, velocity=(3., 4., 0.)):
    omega, radius = 7.292115e-5, 6378137.
    angle = omega*time
    c, s = math.cos(angle), math.sin(angle)
    r = [(radius+height)*c-east*s, (radius+height)*s+east*c, 0.]
    ve, vn, vu = velocity
    v = [-omega*r[1]-ve*s+vu*c, omega*r[0]+ve*c+vu*s, vn]
    return {"time_s": time, "phase": "recovery_entry_coast", "body_id": "booster",
            "r_eci_m": r, "v_eci_mps": v, "q_body_to_eci": [1., 0., 0., 0.],
            "omega_body_rad_s": [0., 0., 0.], "propellant_kg": 50000.,
            "altitude_m": height, "ground_speed_mps": 999., "com_z_m": 32.5,
            "applied_thrust_n": 0., "engine_count": 0, "throttle": 0.,
            "engine_states": [], "flap_angles_rad": [], "contact": False}


def case(label="開発A", *, passed=True, end=103., handoff=False, support=False):
    samples = [sample(100., 0., 1000.), sample(101., 10., 500.), sample(end, 30., 100.)]
    run = {"scenario": "booster_return", "body_id": "booster", "samples": samples,
           "events": [], "final_state": {"propellant_kg": 50000.},
           "outcome": {"termination": "surface_contact", "final_ground_speed_mps": 999.},
           "recovery_record": {"handoff": {"observation": {"position_error_enu_m": [30., 40., 0.]}}}}
    result = {"label": label, "run": run,
            "verification": {"passed": passed, "handoff_reached": handoff,
                             "catch_supported_after_handoff": support},
            "source_receipt": {"source_sha256": {"src/runtime/producer.py": "a"*64},
                               "wall_s": 1.25,
                               "full_launch_reexecuted": False}}
    return bind(result)


def bind(item):
    item["source_receipt"]["return_json_sha256"] = sha256(json.dumps(item["run"], allow_nan=False,
        separators=(",", ":")).encode()).hexdigest()
    item["source_receipt"]["verification"] = deepcopy(item["verification"])
    return item


def parsed(page):
    parser = Scripts()
    parser.feed(page)
    dataset = json.loads(next(text for attrs, text in parser.scripts if attrs.get("id") == "completion-data"))
    return parser, dataset


def test_measured_numbers_verification_denominator_and_source_manifest(profile):
    cases = [case(), case("支持", handoff=True, support=True),
             case("不整合", passed=False, handoff=True, support=True)]
    before = deepcopy(cases)
    page = build_completion_report(cases, profile)
    _, dataset = parsed(page)
    manifest = dataset["manifest"]
    assert manifest["valid_record_count"] == 2
    assert manifest["invalid_record_count"] == 1
    assert manifest["handoff_count"] == manifest["support_count"] == 1
    assert dataset["cases"][2]["handoff"] is False
    assert dataset["cases"][2]["support"] is False
    assert dataset["cases"][0]["points"][-1]["cg_speed_mps"] == pytest.approx(5.)
    assert dataset["cases"][0]["points"][-1]["position_enu_m"] == pytest.approx([30., 0., 100.], abs=1e-8)
    assert "50.00" in page and "5.000" in page and "1.250" in page
    assert "a"*64 in page and cases[0]["source_receipt"]["return_json_sha256"] in page
    assert "有効な未達記録" in page and "整合性未成立・集計除外" in page
    assert manifest["physical_execution"] is False
    assert manifest["mission_completed"] is False
    assert manifest["model_advantage_established"] is False
    assert manifest["campaign_complete_enumeration_established"] is False
    assert cases == before


def test_missing_max_altitude_is_added_only_to_display_copy(profile):
    original = case()
    page = build_completion_report([original], profile)
    parser, _ = parsed(page)
    cg = json.loads(next(text for attrs, text in parser.scripts if attrs.get("id") == "evidence"))
    assert cg["runs"][0]["outcome"]["max_altitude_m"] == 1000.
    assert "max_altitude_m" not in original["run"]["outcome"]
    assert page.index('id="completion-evidence-section"') < page.index('class="hero"')


def test_exact_catch_continuation_is_only_rendered_after_verified_arrival(profile):
    valid = case(handoff=True, support=True)
    valid["catch_run"] = {"initial_state": deepcopy(valid["run"]["final_state"]),
                          "samples": [sample(103., 30., 100.), sample(104., 30., 100.)],
                          "events": [], "outcome": {"termination": "simulated_support", "final_ground_speed_mps": 0.}}
    before = deepcopy(valid)
    parser, dataset = parsed(build_completion_report([valid], profile))
    cg = json.loads(next(text for attrs, text in parser.scripts if attrs.get("id") == "evidence"))
    assert cg["runs"][0]["samples"][-1]["time_s"] == 104.
    assert cg["runs"][0]["outcome"]["termination"] == "simulated_support"
    assert cg["runs"][0]["launch_continuation"] is False
    assert dataset["manifest"]["cases"][0]["exact_catch_state_continuation_rendered"] is True
    assert valid == before
    valid["catch_run"]["initial_state"]["propellant_kg"] = 1.
    parser, dataset = parsed(build_completion_report([valid], profile))
    cg = json.loads(next(text for attrs, text in parser.scripts if attrs.get("id") == "evidence"))
    assert cg["runs"][0]["samples"][-1]["time_s"] == 103.
    assert dataset["manifest"]["cases"][0]["exact_catch_state_continuation_rendered"] is False


def test_cg_reduction_preserves_endpoints_and_actual_event_nearest_saved_poses(profile):
    original = case()
    original["run"]["samples"] = [sample(round(100.+i*.1, 8), float(i), 1000.-i) for i in range(22)]
    original["run"]["events"] = [{"time_s": 100.23, "event": "between_saved_samples"},
                                  {"time_s": 100.7, "event": "exact_saved_sample"}]
    bind(original)
    before = deepcopy(original)
    parser, dataset = parsed(build_completion_report([original], profile))
    cg = json.loads(next(text for attrs, text in parser.scripts if attrs.get("id") == "evidence"))
    timestamps = [row["time_s"] for row in cg["runs"][0]["samples"]]
    assert timestamps[0] == 100. and timestamps[-1] == 102.1
    assert 100.2 in timestamps and 100.7 in timestamps
    metadata = dataset["manifest"]["cases"][0]
    assert metadata["cg_raw_sample_count"] == 22
    assert metadata["cg_display_sample_count"] == len(timestamps) < 22
    assert metadata["cg_display_rule"]["normal_min_interval_s"] == .5
    assert metadata["cg_event_sample_bindings"] == [
        {"event_index": 0, "event_time_s": 100.23, "saved_sample_time_s": 100.2, "exact": False},
        {"event_index": 1, "event_time_s": 100.7, "saved_sample_time_s": 100.7, "exact": True}]
    assert metadata["full_metrics_use_reduced_cg"] is False
    assert len(dataset["cases"][0]["points"]) == 22
    assert original == before


def test_catch_saved_frames_and_poses_retain_point_one_second_gap_bound(profile):
    original = case(handoff=True, support=True)
    configuration = json.loads((Path(__file__).resolve().parents[2]/"examples"/"spaceflight"/"starship-catch-profile.json").read_text())
    original["catch_run"] = {"initial_state": deepcopy(original["run"]["final_state"]),
        "samples": [sample(round(103.+i*.01, 8), 30., 100.) for i in range(51)], "events": [],
        "outcome": {"termination": "simulated_support", "final_ground_speed_mps": 0.},
        "catch_record": {"configuration": configuration, "frames": [
            {"time_s": round(103.+i*.01, 8), "r_eci_m": sample(103.+i*.01, 30., 100.)["r_eci_m"],
             "q_body_to_eci": [1., 0., 0., 0.]} for i in range(51)]}}
    before = deepcopy(original)
    parser, dataset = parsed(build_completion_report([original], profile))
    cg = json.loads(next(text for attrs, text in parser.scripts if attrs.get("id") == "evidence"))
    near = [s for s in cg["runs"][0]["samples"] if s["time_s"] >= 103.]
    frames = cg["runs"][0]["catch_record"]["frames"]
    assert near[0]["time_s"] == frames[0]["time_s"] == 103.
    assert near[-1]["time_s"] == frames[-1]["time_s"] == 103.5
    assert max(b["time_s"]-a["time_s"] for a, b in zip(near, near[1:])) <= .1+1e-9
    assert max(b["time_s"]-a["time_s"] for a, b in zip(frames, frames[1:])) <= .1+1e-9
    assert len(frames) < 51
    assert dataset["manifest"]["cases"][0]["cg_raw_catch_frame_count"] == 51
    assert original == before


def test_lossless_cg_encoding_restores_exact_engine_and_shared_geometry_values(profile):
    original = case()
    engines = [{"throttle": .4, "gimbal_x_rad": 1.23456789e-5, "gimbal_y_rad": -.032345678,
                "available": True}, {"throttle": 0., "gimbal_x_rad": 0., "gimbal_y_rad": 0., "available": False}]
    anchors = [[1.23456789123, 0., 0.], [-1.23456789123, 0., 0.]]
    for row in original["run"]["samples"]:
        row.update(engine_states=deepcopy(engines), main_engine_anchors=deepcopy(anchors),
                   main_engine_thrust_n=[2451662.5, 0.], aero_panel_names=["hull", "fin"])
    bind(original)
    before = deepcopy(original)
    parser, dataset = parsed(build_completion_report([original], profile))
    cg = json.loads(next(text for attrs, text in parser.scripts if attrs.get("id") == "evidence"))
    assert cg["runs"][0]["samples"][0]["engine_states"][0] == [.4, 1.23456789e-5, -.032345678, True]
    assert "main_engine_anchors" not in cg["runs"][0]["samples"][0]
    assert dataset["manifest"]["cases"][0]["cg_lossless_encoding"]["numeric_values_rounded"] is False
    node = shutil.which("node")
    if node:
        decoder = next(text for attrs, text in parser.scripts if "study.runs.forEach((run,index)" in text)
        script = "global.document={getElementById:()=>({textContent:"+json.dumps(json.dumps(cg))+"})};global.window={};\n"+decoder
        script += "console.log(JSON.stringify(window.completionCGStudy.runs[0].samples[0]));"
        result = subprocess.run([node], input=script, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        decoded = json.loads(result.stdout)
        assert decoded["engine_states"] == engines
        assert decoded["main_engine_anchors"] == anchors
    assert original == before


@pytest.mark.parametrize("error", ["reverse", "duplicate", "nan", "bad_quaternion"])
def test_invalid_saved_positions_are_visible_and_not_counted(profile, error):
    invalid = case("履歴不正", passed=True, handoff=True, support=True)
    samples = invalid["run"]["samples"]
    if error == "reverse":
        samples[1]["time_s"] = 99.
    elif error == "duplicate":
        samples[1]["time_s"] = 100.
    elif error == "nan":
        samples[1]["r_eci_m"][0] = float("nan")
    else:
        samples[1]["q_body_to_eci"] = [2., 0., 0., 0.]
    page = build_completion_report([invalid], profile)
    _, dataset = parsed(page)
    assert dataset["manifest"]["valid_record_count"] == 0
    assert dataset["manifest"]["support_count"] == 0
    assert dataset["cases"][0]["display_error"]
    assert dataset["cases"][0]["points"] == []
    assert "整合性未成立・集計除外" in page


def test_support_requires_arrival_and_full_launch_requires_enclosing_proof(profile):
    unsupported = case(handoff=False, support=True)
    unsupported["source_receipt"]["full_launch_reexecuted"] = True
    _, result = parsed(build_completion_report([unsupported], profile))
    assert result["manifest"]["support_count"] == 0
    assert result["manifest"]["full_launch_verified_count"] == 0
    unsupported["source_receipt"]["enclosing_verification"] = {"passed": True}
    page = build_completion_report([unsupported], profile)
    _, result = parsed(page)
    assert result["manifest"]["full_launch_verified_count"] == 0
    assert result["cases"][0]["full_launch_proof"] is False
    assert result["cases"][0]["caller_declared_full_launch"] is True
    assert "呼出側の宣言・未検証" in page


def test_different_initial_states_remain_explicitly_unmatched(profile):
    first, second = case(), case("別開始")
    second["run"]["samples"][0] = sample(100., 100., 1000.)
    bind(second)
    page = build_completion_report([first, second], profile)
    _, dataset = parsed(page)
    assert dataset["manifest"]["same_exact_initial_state"] is False
    assert "同条件比較ではありません" in page
    assert "固定条件の頑健性試験" in page


def test_labels_and_source_receipts_cannot_close_script_or_inject_markup(profile):
    payload = '</script><script>alert("injected")</script><img src=x onerror=alert(1)>'
    injected = case(payload)
    injected["source_receipt"]["note"] = payload
    page = build_completion_report([injected], profile)
    parser, dataset = parsed(page)
    assert dataset["cases"][0]["label"] == payload
    assert payload not in page
    assert not any('alert("injected")' in text for attrs, text in parser.scripts if attrs.get("type") != "application/json")
    assert "fetch(" not in next(text for attrs, text in parser.scripts if attrs.get("id") is None and "completionReplayDiagnostics" in text)


def test_standalone_scripts_parse_and_saved_point_controls_stop_at_each_terminal(profile):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node parser unavailable")
    page = build_completion_report([case(), case("短い記録", end=102.)], profile)
    parser, dataset = parsed(page)
    scripts = [text for attrs, text in parser.scripts if attrs.get("type") != "application/json"]
    check = subprocess.run([node, "--check"], input="\n".join(scripts), text=True, capture_output=True)
    assert check.returncode == 0, check.stderr
    replay = next(text for text in scripts if "completionReplayDiagnostics" in text)
    harness = r"""
const noop=()=>{},ctx=new Proxy({},{get:()=>noop});
const elements={};
for(const id of ['completion-case','completion-time','completion-play','completion-reset','completion-clock','completion-export','completion-left','completion-right','completion-left-title','completion-right-title','completion-left-state','completion-right-state','completion-evidence-section'])elements[id]={value:'0',max:'0',clientWidth:640,clientHeight:300,dataset:{},handlers:{},append:noop,setAttribute:noop,getContext:()=>ctx,addEventListener(name,fn){this.handlers[name]=fn}};
elements['completion-data']={textContent:DATA};
global.document={getElementById:id=>elements[id]||null,createElement:()=>({})};
global.window={devicePixelRatio:1,matchMedia:()=>({matches:false}),addEventListener:noop};
""".replace("DATA", json.dumps(json.dumps(dataset)))
    harness += replay
    harness += r"""
elements['completion-time'].value='1.25';elements['completion-time'].handlers.input();const middle=window.completionReplayDiagnostics;
elements['completion-time'].value=elements['completion-time'].max;elements['completion-time'].handlers.input();const ending=window.completionReplayDiagnostics;
elements['completion-reset'].handlers.click();const reset=window.completionReplayDiagnostics;
console.log(JSON.stringify({middle,ending,reset}));
"""
    result = subprocess.run([node], input=harness, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["middle"]["left"]["time_s"] == observed["middle"]["right"]["time_s"] == 101.
    assert observed["middle"]["interpolated"] is False
    assert observed["ending"]["left"]["time_s"] == 103.
    assert observed["ending"]["right"]["time_s"] == 102.
    assert observed["ending"]["left"]["terminal"] and observed["ending"]["right"]["terminal"]
    assert observed["reset"]["elapsed_s"] == 0
    assert "prefers-reduced-motion" in page and "focus-visible" in page and "max-width:736px" in page


def test_empty_report_is_refused(profile):
    with pytest.raises(ValueError, match="requires_cases"):
        build_completion_report([], profile)


@pytest.mark.parametrize("mutation", ("missing_hash", "wrong_hash", "missing_verdict", "wrong_verdict", "boolean_numeric"))
def test_missing_or_mismatched_record_verification_binding_is_visible_and_excluded(profile, mutation):
    original = case(handoff=True, support=True)
    receipt = original["source_receipt"]
    if mutation == "missing_hash":
        del receipt["return_json_sha256"]
    elif mutation == "wrong_hash":
        receipt["return_json_sha256"] = "0"*64
    elif mutation == "missing_verdict":
        del receipt["verification"]
    elif mutation == "wrong_verdict":
        receipt["verification"]["handoff_reached"] = False
    else:
        receipt["verification"]["passed"] = 1
    page = build_completion_report([original], profile)
    _, result = parsed(page)
    assert result["manifest"]["valid_record_count"] == result["manifest"]["handoff_count"] == result["manifest"]["support_count"] == 0
    assert result["manifest"]["invalid_record_count"] == 1
    assert result["manifest"]["cases"][0]["record_binding_error"]
    assert result["cases"][0]["valid"] is False and result["cases"][0]["points"]
    assert "整合性未成立・集計除外" in page


def test_raw_hash_uses_producer_ascii_serialization_not_presentation_json(profile):
    original = case()
    original["run"]["note"] = "保存状態 <確認> 日本語"
    bind(original)
    page = build_completion_report([original], profile)
    _, result = parsed(page)
    assert result["manifest"]["valid_record_count"] == 1
    assert result["manifest"]["cases"][0]["return_json_sha256_reconstructed"] == original["source_receipt"]["return_json_sha256"]
    assert "表示対象の記録" in page and "一覧が試験全体を網羅することも確認しません" in page
