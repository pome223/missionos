#!/usr/bin/env python3
"""Allowlist saved numerical records into a portable display, never run physics.

With no external paths, --check and --rebuild-page use the shipped dataset.
External input files are opt-in; source bytes/paths are never copied wholesale.
"""
import argparse
import bisect
import gzip
import hashlib
import json
import math
import subprocess
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIELDS = ["time_s", "phase", "body_id", "r_eci_m", "v_eci_mps", "q_body_to_eci",
          "omega_body_rad_s", "propellant_kg", "altitude_m", "ground_speed_mps",
          "com_z_m", "applied_thrust_n", "throttle", "engine_states", "flap_angles_rad",
          "main_engine_thrust_n", "scene_index"]
ENGINE_FIELDS = ["throttle", "gimbal_x_rad", "gimbal_y_rad", "available"]


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def load(path):
    raw = path.read_bytes()
    decoded = gzip.decompress(raw) if path.suffix == ".gz" else raw
    return json.loads(decoded), {"file_sha256": sha(raw), "decoded_bytes_sha256": sha(decoded)}


def select(rows, lo, hi):
    """Retain one saved bracket on each side; never extrapolate clip motion."""
    times = [s["time_s"] for s in rows]
    left = max(0, bisect.bisect_left(times, lo) - 1)
    right = min(len(rows), bisect.bisect_right(times, hi) + 1)
    return rows[left:right]


def pack(rows):
    if any(b["time_s"] < a["time_s"] for a, b in zip(rows, rows[1:])):
        raise ValueError("reversed_saved_time_is_not_display_data")
    input_count = len(rows)
    rows = list({s["time_s"]: s for s in rows}.values())
    scenes, output = [], []
    for sample in rows:
        scene = {k: sample.get(k, []) for k in ("main_engine_anchors", "aero_panel_names")}
        if scene not in scenes:
            scenes.append(scene)
        clean = {k: sample.get(k) for k in FIELDS}
        clean["scene_index"] = scenes.index(scene)
        engines = sample.get("engine_states", [])
        count = len(scene["main_engine_anchors"])
        clean["engine_states"] = [[e[k] for k in ENGINE_FIELDS] for e in engines[:count]]
        clean["main_engine_thrust_n"] = sample.get("main_engine_thrust_n", [])
        output.append([clean[k] for k in FIELDS])
    return {"fields": FIELDS, "engine_fields": ENGINE_FIELDS, "scenes": scenes, "rows": output,
            "coincident_saved_rows_selected_last": input_count - len(rows)}


def source_fact(path, label, extra=None):
    value, hashes = load(path)
    return value, {"public_case_id": label, **hashes, **(extra or {})}


def outcome(run):
    raw = run["outcome"]
    result = {k: raw[k] for k in ("termination", "duration_s", "payload_released_count",
        "integration_steps", "handoff_reached", "full_launch_reexecuted", "catch_executed",
        "final_hull_clearance_m") if k in raw}
    contact = raw.get("contact_receipt")
    if contact:
        result["contact_point_speed_mps"] = contact["surface_relative_speed_mps"]
        result["landing_verified"] = contact["landing_verified"]
    result.update(physical_execution=False, mission_completed=False, vehicle_validated=False)
    return result


def build(args):
    nominal, nominal_source = source_fact(args.nominal, "nominal-historical-sixdof")
    supervised, supervision_source = source_fact(args.supervision, "live-routing-historical-sixdof")
    terminal, terminal_source = source_fact(args.return_record, "isolated-return-18")
    n, s, r = nominal["runs"][0], supervised["runs"][0], terminal["run"]
    nv, nominal_verification = source_fact(args.nominal_verification, "nominal-stored-consistency")
    sv, supervision_verification = source_fact(args.supervision_verification, "supervision-stored-consistency")
    rv, return_verification = source_fact(args.return_verification, "return-18-stored-causal-arithmetic")
    if not (nv.get("passed") and sv.get("passed") and rv["verification"].get("source_bound_arithmetic_passed")):
        raise ValueError("saved_verdicts_required_before_display_export")
    ranges = [(0, 150), (520, 910), (4700, n["samples"][-1]["time_s"])]
    # The long intervening orbit is retained as sparse ORIGINAL samples; cuts
    # choose separate intervals. All frames are saved samples, not new states.
    selected = {x["time_s"]: x for lo, hi in ranges for x in select(n["samples"], lo, hi)}
    nominal_rows = [selected[t] for t in sorted(selected)]
    satellites = [{"id": b["id"], "release_s": b["samples"][0]["time_s"],
        "frames": pack(select(b["samples"], 520, 910))} for b in n["satellites"]]
    sup = s["supervision"]
    response = sup["response"]
    timeline = [{k: x[k] for k in ("time_s", "sequencer_state", "payload_released_count", "release_attempt_count",
        "release_acknowledged") if k in x} for x in sup["observations"]]
    command = sup["commands"][0]
    decision = {"action": response["action"], "route": response["route"],
        "command_time_s": command["time_s"], "rule_allowed": sup["rules"][0]["allowed"],
        "command_accepted": command["accepted"],
        "jev_inference_invoked": response["jev_invocation"]["model_inference_invoked"],
        "deepseek_inference_invoked": response["llm_invocation"]["model_inference_invoked"],
        "observed_sequencer_effect": sv["flight_supervision"]["observed_effect"],
        "approval_is_model_decision": False, "authenticated_human_identity_verified": False,
        "test_operator": True, "model_value_demonstrated": False}
    final = r["recovery_record"]["handoff"]
    cases = [
        {"id": "nominal-flight", "title": "Saved 6DOF · launch, deployment, return",
         "caption": "Historical numerical replay · 26 separated bodies · contact is not verified landing",
         "frames": pack(nominal_rows), "satellites": satellites,
         "events": [{"time_s": e["time_s"], "event": e["event"]} for e in n["events"]],
         "segments": [{"start_s": lo, "end_s": hi, "video_s": duration, "label": label, "camera": camera}
             for (lo, hi), duration, label, camera in zip(ranges, (12, 14, 10),
                 ("LAUNCH / STAGE SEPARATION", "26 BODY RELEASES / NO SERVICE VERIFICATION", "SHIP RETURN / CONTACT, NOT VERIFIED LANDING"),
                 ("ground", "payload", "chase"))], "outcome": outcome(n)},
        {"id": "deployment-supervision", "title": "Observed fault → bounded skip → later effect",
         "caption": "1 live Jev call · 0 DeepSeek calls · 0 released payloads · mission incomplete",
         "frames": pack(select(s["samples"], 520, 562)), "satellites": [], "timeline": timeline, "decision": decision,
         "events": [{"time_s": e["time_s"], "event": e["event"]} for e in s["events"] if 520 <= e["time_s"] <= 562],
         "segments": [{"start_s": 520, "end_s": 562, "video_s": 28, "label": "JEV ROUTES · HUMAN SCOPE · RULES · EXECUTOR · VERIFIER", "camera": "payload"}],
         "outcome": outcome(s)},
        {"id": "return-negative", "title": "Isolated saved-state return · objective unmet",
         "caption": "Saved return-state continuation · 600 steps / 60 s · no handoff or support · no physical execution",
         "origin_checkpoint": "CP2711; isolated historical development continuation, not a new full launch",
         "frames": pack(r["samples"]), "satellites": [],
         "segments": [{"start_s": r["samples"][0]["time_s"], "end_s": r["samples"][-1]["time_s"],
             "video_s": 30, "label": "DEVELOPMENT CONTINUATION / NO CATCH EXECUTION", "camera": "orbit"}],
         "events": [{"time_s": e["time_s"], "event": e["event"]} for e in r["events"]], "outcome": outcome(r),
         "terminal": {"fuel_kg": r["final_state"]["propellant_kg"], "reserve_kg": final["limits"]["propellant_reserve_kg"],
             "handoff": final["eligible"], "support": False,
             "final_pin_positions_enu_m": [x["position_enu_m"] for x in final["observation"]["pins"]]}}
    ]
    source_maps = {"nominal": nominal["provenance"]["source_sha256"],
                   "supervision": supervised["provenance"]["source_sha256"],
                   "return_measurement_source_map_sha256": rv["measurement_source_map_sha256"]}
    data = {"schema": "missionos.starship_saved_public_media.v1", "cases": cases,
            "coordinate_space": "recorded ECI metres; renderer east/up/minus-north",
            "quaternion": "scalar-first Hamilton body-to-ECI", "new_physics_states_created": False,
            "physical_execution": False, "mission_completed": False}
    manifest = {"schema": "missionos.starship_saved_public_media_manifest.v1",
        "sources": [nominal_source, supervision_source, terminal_source],
        "saved_verdicts": [nominal_verification, supervision_verification, return_verification],
        "historical_source_versions": source_maps,
        "return_verifier_sha256": rv["verifier_sha256"],
        "verification_scope": "stored-output consistency / saved causal arithmetic; no new integration, vehicle certification, human authentication or physical admission",
        "authority": "LLM judges. Human approves. Rules constrain. Executor acts. Verifier checks. Repair loops.",
        "redaction": "allowlist numerical display fields, semantic events, safe status facts; exclude controller/request/IDs/keys/HMAC/private paths/provider payloads",
        "display": {"numeric_values_rounded": False, "downsampling": "nominal and supervision clip windows with original bracketing samples; return all 601 original samples",
            "coincident_event_times": "last saved post-event row selected for drawing; zero duration or stability credit",
            "seek_policy": "video elapsed time maps only to declared clip windows; jumps over omitted orbit intervals; never interpolates through gaps",
            "engine_states": "lossless columns; only main engines drawn", "interpolation": "display-only linear position and quaternion slerp; never an executed state",
            "illustrative_geometry_and_plumes": True, "satellite_service_verified": False},
        "operator_fixture": {"duration_s": 30.0, "scenario": "gimbal_step", "verified_runtime_smoke": bool(args.operator_receipt),
            "test_operator": True, "authenticated_human_identity_verified": False,
            "historical_receipt": True, "current_public_runtime_smoke_included": False,
            "provider_inference": False, "full_launch_reexecuted": False, "physical_execution": False,
            "note": "Separate initialized operator fixture; not this nominal flight or return continuation"},
        "physical_execution": False, "mission_completed": False, "model_value_demonstrated": False,
        "source_authenticated": False, "runtime_invocation_independently_verified": False,
        "media_maximum_total_bytes": 30 * 1024 * 1024}
    if args.operator_receipt:
        op, fact = source_fact(args.operator_receipt, "separate-operator-runtime-fixture")
        if not (op["verified"] and op["duration_s"] == 30 and op["physical_execution"] is False):
            raise ValueError("declared_operator_fixture_must_match_saved_receipt")
        manifest["operator_fixture"]["receipt_file_sha256"] = fact["file_sha256"]
        manifest["operator_fixture"]["study_file_sha256"] = op["artifacts"]["study.json"]["sha256"]
    manifest["saved_verdict_results"] = {
        "nominal": {"passed": nv["passed"], "scope": nv["scope"]},
        "supervision": {"passed": sv["passed"], "observed_effect": sv["flight_supervision"]["observed_effect"],
            "scope": sv["flight_supervision"]["scope"]},
        "return": {k: rv["verification"][k] for k in ("passed", "arithmetic_record_integrity_passed",
            "source_bound_arithmetic_passed", "positive_time_transitions_checked", "durable_command_attempts_checked",
            "dynamics_replayed", "source_authenticated", "physical_invocation_admitted", "arrival_admitted", "support_admitted")}}
    return data, manifest


def validate(data):
    if data.get("schema") != "missionos.starship_saved_public_media.v1" or len(data["cases"]) != 3:
        raise ValueError("closed_media_case_set_required")
    for case in data["cases"]:
        for frames in [case["frames"], *(x["frames"] for x in case["satellites"])]:
            times = [row[0] for row in frames["rows"]]
            if not times or any(b <= a for a, b in zip(times, times[1:])):
                raise ValueError("finite_ordered_saved_samples_required")
            for row in frames["rows"]:
                if len(row) != len(FIELDS):
                    raise ValueError("lossless_columns_required")
                q = row[FIELDS.index("q_body_to_eci")]
                if len(q) != 4 or abs(math.sqrt(sum(x*x for x in q))-1) > 1e-6:
                    raise ValueError("recorded_unit_quaternion_required")
    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("nonfinite_public_display_value")
        if isinstance(value, dict):
            for k, v in value.items():
                if k in {"request_id", "command_id", "session_id", "run_id", "controller", "command", "signature", "token", "api_key"}:
                    raise ValueError("private_field_not_public_display")
                finite(v)
        elif isinstance(value, list):
            for v in value:
                finite(v)
    finite(data)


def page(data):
    template = (HERE / "page-template.html").read_text()
    scripts = "\n".join((HERE / f).read_text() for f in ("starship_cg_models.js", "starship_cg_renderer.js", "replay.js"))
    return template.replace("__DATA__", encoded(data).decode().replace("<", "\\u003c")).replace("__SCRIPTS__", scripts)


def serve(port):
    """Optional local UI export writer, bounded to six generated filenames."""
    names = {case+extension for case in ("nominal-flight", "deployment-supervision", "return-negative")
             for extension in (".png", ".webm")}
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(HERE), **kwargs)

        def do_POST(self):
            name = self.path.removeprefix("/saved-media-export/")
            length = int(self.headers.get("Content-Length", "0"))
            expected_origin = f"http://127.0.0.1:{port}"
            if (not self.path.startswith("/saved-media-export/") or name not in names
                    or not 0 < length <= 12 * 1024 * 1024 or self.headers.get("Origin") != expected_origin):
                self.send_error(400, "fixed generated media filename, local origin and bounded bytes required")
                return
            raw = self.rfile.read(length)
            magic = b"\x89PNG\r\n\x1a\n" if name.endswith(".png") else b"\x1a\x45\xdf\xa3"
            if len(raw) != length or not raw.startswith(magic):
                self.send_error(400, "generated PNG or WebM container required")
                return
            captures = HERE / "captures"
            captures.mkdir(exist_ok=True)
            try:
                with (captures / name).open("xb") as stream:
                    stream.write(raw)
            except FileExistsError:
                self.send_error(409, "existing capture is retained; no overwrite")
                return
            response = encoded({"saved_filename": name, "bytes": length})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
    print(f"Saved-media UI only: http://127.0.0.1:{port}; no model, simulation or hardware API")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def previews(manifest, ffmpeg):
    """Transform existing MP4 bytes only; no CG capture or model invocation."""
    selections = [("nominal-flight", 0.0, 12.0, 1.0, "launch and stage-separation excerpt"),
                  ("deployment-supervision", 3.5, 24.5, 2.0, "observations, skip command and both later-effect observations"),
                  ("return-negative", 18.0, 12.0, 1.0, "final approach, reserve crossing and unsuccessful endpoint")]
    entries = []
    for case, start, interval, speed, description in selections:
        movie = HERE / (case + ".mp4")
        original = next(x for x in manifest["media"] if x["filename"] == movie.name)
        if sha(movie.read_bytes()) != original["sha256"]:
            raise ValueError("preview_requires_unchanged_source_movie")
        target = HERE / (case + ".gif")
        filters = (f"[0:v]setpts=PTS/{speed},fps=6,scale=640:-1:flags=lanczos,"
                   "drawtext=font=Arial:text='Animated preview / full MP4 linked':"
                   "fontsize=10:fontcolor=white:x=16:y=48:box=1:boxcolor=black@0.7,"
                   "split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];"
                   "[b][p]paletteuse=dither=sierra2_4a")
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-n", "-ss", str(start),
                   "-t", str(interval), "-i", str(movie), "-filter_complex", filters,
                   "-an", "-loop", "2", str(target)]
        subprocess.run(command, check=True)
        if target.stat().st_size > 3 * 1024 * 1024:
            raise ValueError("preview_exceeds_declared_three_MiB_cap")
        entries.append({"filename": target.name, "bytes": target.stat().st_size, "sha256": sha(target.read_bytes()),
            "source_movie_filename": movie.name, "source_movie_sha256": original["sha256"],
            "source_movie_start_s": start, "source_movie_interval_s": interval,
            "preview_speed_vs_source_movie": speed, "target_fps": 6, "width": 640, "height": 360,
            "loop_repetitions": 2, "caption": "Animated preview; full MP4 linked", "excerpt": description,
            "transformation": "FFmpeg saved-MP4 excerpt, display-time rescale, 128-colour palette and label; no new rendered physical state"})
    manifest["media"] += entries
    manifest["public_media_total_bytes"] = sum(x["bytes"] for x in manifest["media"])
    if manifest["public_media_total_bytes"] > manifest["media_maximum_total_bytes"]:
        raise ValueError("all_public_media_exceed_thirty_MiB_cap")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("nominal", "supervision", "return-record", "nominal-verification", "supervision-verification", "return-verification", "operator-receipt"):
        parser.add_argument("--"+name, type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--rebuild-page", action="store_true")
    parser.add_argument("--serve", action="store_true", help="loopback-only optional CUA/browser MediaRecorder export writer")
    parser.add_argument("--port", type=int, default=18766)
    parser.add_argument("--make-previews", action="store_true", help="create three labelled GIF excerpts from existing MP4s only")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="installed FFmpeg executable for the opt-in preview transform")
    args = parser.parse_args()
    if args.nominal:
        if not all((args.supervision, args.return_record, args.nominal_verification, args.supervision_verification, args.return_verification)):
            parser.error("external curation requires all three saved records and saved verdicts")
        data, manifest = build(args)
        validate(data)
        (HERE / "data.json").write_bytes(encoded(data))
        manifest["curated_dataset_sha256"] = sha(encoded(data))
        manifest["renderer_sources_sha256"] = {f: sha((HERE / f).read_bytes()) for f in
            ("starship_cg_models.js", "starship_cg_renderer.js", "replay.js", "page-template.html", "export_saved_media.py")}
        (HERE / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n")
        (HERE / "index.html").write_text(page(data))
    else:
        data = json.loads((HERE / "data.json").read_bytes())
        validate(data)
        manifest = json.loads((HERE / "manifest.json").read_bytes())
        if manifest["curated_dataset_sha256"] != sha(encoded(data)):
            raise ValueError("curated_dataset_manifest_hash_mismatch")
        if args.rebuild_page:
            (HERE / "index.html").write_text(page(data))
            manifest["renderer_sources_sha256"] = {f: sha((HERE / f).read_bytes()) for f in
                ("starship_cg_models.js", "starship_cg_renderer.js", "replay.js", "page-template.html", "export_saved_media.py")}
            manifest["standalone_page_sha256"] = sha((HERE / "index.html").read_bytes())
            (HERE / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n")
    if args.make_previews:
        manifest = previews(manifest, args.ffmpeg)
        (HERE / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n")
    if args.check:
        for name, expected in manifest["renderer_sources_sha256"].items():
            if sha((HERE / name).read_bytes()) != expected:
                raise ValueError("renderer_source_manifest_hash_mismatch")
        if manifest.get("standalone_page_sha256") and sha((HERE / "index.html").read_bytes()) != manifest["standalone_page_sha256"]:
            raise ValueError("standalone_page_manifest_hash_mismatch")
        for entry in manifest.get("media", []):
            if sha((HERE / entry["filename"]).read_bytes()) != entry["sha256"]:
                raise ValueError("public_media_manifest_hash_mismatch")
        print(json.dumps({"passed": True, "case_sample_counts": {c["id"]: len(c["frames"]["rows"]) for c in data["cases"]},
                          "new_model_simulator_or_hardware_calls": 0, "physical_execution": False}))
    if args.serve:
        serve(args.port)


if __name__ == "__main__":
    main()
