"""One source-bound fixture flight through the existing M2/return executor."""

from copy import deepcopy
import base64
from hashlib import sha256
import html
import json
import os
from pathlib import Path
import tempfile
import time

from .starship_artifacts import write_verified_input
from .starship_visual_contract import (ROOT, CASES, digest, identity, sources,
                                      visual_contract)

REGISTRATION = "examples/spaceflight/starship-visual-observation-registration.json"


def replace_derived(path, data):
    """Atomically publish a recomputed verdict/report, never overwrite inputs."""
    path = Path(path)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError("visual_derived_artifact_symlink")
    fd, pending = tempfile.mkstemp(prefix="."+path.name+"-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)


def write_manifest(output, run_id):
    output = Path(output)
    manifest = {"schema": "missionos.starship_visual_artifacts.v1", "run_id": run_id,
                "files": {name: sha256((output / name).read_bytes()).hexdigest()
                          for name in ("study.json", "verification.json", "report.html", "plan.json",
                                       "runtime-budget-failure.json") if (output / name).exists()}}
    replace_derived(output / "manifest.json", (json.dumps(manifest, indent=2, allow_nan=False)+"\n").encode())


def prepare(case, *, run_id):
    """Freeze one reviewable invocation; preparation grants no execution."""
    if not identity(run_id) or len(run_id) > 80:
        raise ValueError("visual_run_identity")
    from .starship_m2_contract import PROFILE
    registration = json.loads((ROOT / REGISTRATION).read_text())
    if registration["cases"] != list(CASES) or registration["mode"] != "fixture":
        raise ValueError("visual_registration_changed")
    profile = json.loads((ROOT / PROFILE).read_text())
    plan = {"schema": "missionos.starship_visual_plan.v1", "run_id": run_id,
            "authority": visual_contract(case), "profile_sha256": digest(profile),
            "source_sha256": sources(), "registration_sha256": digest(registration),
            "execution_authorized": False, "maximum_hosted_model_calls": 0}
    plan["plan_sha256"] = digest(plan)
    return plan


def validate_plan(plan):
    if type(plan) is not dict or plan != prepare(plan["authority"]["case"], run_id=plan["run_id"]):
        raise ValueError("visual_plan_stale_or_changed")
    return deepcopy(plan)


def fixture_choices(case):
    """Registered scripted judgments; the HTTP fixture never reads world truth."""
    visible = {"visibility": "adequate", "deployment_visual_state": "separation_visible",
               "telemetry_relation": "insufficient", "frame_support": "supports"}
    if case == "normal":
        return []
    if case == "retained_payload":
        return [{**visible, "deployment_visual_state": "suspected_retention",
                 "recommended_action": "stop_deployment", "observation_choice": "none"}]
    if case == "primary_occluded":
        return [{"visibility": "occluded", "deployment_visual_state": "indeterminate",
                 "telemetry_relation": "insufficient", "frame_support": "uncertain",
                 "recommended_action": "request_visual_observation", "observation_choice": "alternate_camera"},
                {**visible, "telemetry_relation": "consistent", "recommended_action": "continue",
                 "observation_choice": "none"}]
    return [{**visible, "recommended_action": "request_numeric_confirmation",
             "observation_choice": "fresh_separation_counter"},
            {"telemetry_relation": "consistent", "recommended_action": "continue",
             "observation_choice": "none"}]


def run_fixture(plan, output):
    plan = validate_plan(plan)
    authority, run_id = plan["authority"], plan["run_id"]
    output = Path(output)
    if output.exists() or any(p.is_symlink() for p in (output, *output.parents)):
        raise ValueError("visual_fresh_owned_output_required")
    from .starship_m2_contract import PROFILE
    from .starship_return_feasibility import readiness
    from .starship_replanning import source_hashes as return_sources
    from .starship_deployment_world import DeploymentWorld
    from .starship_visual_observation import VisualObservationCollector, default_cameras
    from .starship_visual_director import VisualRuntime
    from .starship_m2_runner import run_simulation
    from src.intelligence.starship_visual_agent import VisualAgent
    from src.intelligence.starship_visual_transport import (
        VisualAttemptLedger, VisualTransport, fixture_provider,
    )

    profile = json.loads((ROOT / PROFILE).read_text())
    output.mkdir(parents=True, mode=0o700)
    write_verified_input(output / "plan.json", plan)
    study = {"schema": "missionos.starship_visual_study.v1", "run_id": run_id,
             "authority": authority, "profile": profile, "plan": plan,
             "source_sha256": plan["source_sha256"], "return_source_sha256": return_sources(),
             "m2": None, "visual": None, "http_fixture_requests": [], "attempt_ledger": None,
             "runtime": {"hosted_model_calls": 0, "physical_execution": False,
                         "automatic_retry": False, "boundary": "loopback_http_visual_to_m2_return"}}
    started, runtime, collector = time.monotonic(), None, None
    try:
        _, _, reason = readiness(profile)
        if reason:
            raise ValueError(reason)
        cameras = default_cameras()
        if authority["case"] == "primary_occluded":
            cameras[0]["lens_covered"] = True
        cfg = authority["visual"]
        collector = VisualObservationCollector(DeploymentWorld(profile), output / "camera", run_id=run_id,
            cameras=cameras, config={"fps": cfg["camera_fps"], "max_age_s": cfg["frame_max_age_s"],
                                    "history_s": cfg["ring_duration_s"], "ring_bytes": cfg["maximum_ring_bytes"],
                                    "store_bytes": cfg["maximum_store_bytes"]})
        runtime = VisualRuntime(run_id=run_id, authority=authority, collector=collector)
        ledger = VisualAttemptLedger(output / "visual-attempts.json", run_id=run_id,
                                     authority_sha256=digest(authority), mode="loopback_fixture")
        with fixture_provider(fixture_choices(authority["case"])) as provider:
            transport = VisualTransport(mode="loopback_fixture", fixture_endpoint=provider["endpoint"],
                ledger=ledger, active=runtime.request_active, remaining=runtime.remaining_s)
            runtime.agent = VisualAgent(transport=transport, frame_store=collector.store)
            inner = authority["m2_authority"]
            study["m2"] = run_simulation(profile, inner["case"], output / "mission", mode="fixture",
                                          authority=inner, run_id=run_id, visual_runtime=runtime)
            study["http_fixture_requests"] = deepcopy(provider["requests"])
        if (output / "visual-attempts.json").exists():
            study["attempt_ledger"] = json.loads((output / "visual-attempts.json").read_text())
    except Exception as error:
        study["runtime_failure"] = {"type": type(error).__name__, "reason": str(error), "retry_performed": False}
    finally:
        if runtime is not None:
            study["visual"] = runtime.finish()
        if collector is not None:
            study["camera"] = {"cameras": collector.cameras, "config": collector.config,
                               "capture_count": collector._sequence,
                               "recent_capture_receipts": list(collector.capture_receipts),
                               "renderer": "numpy_triangle_perspective_zbuffer_v1"}
        study["runtime"].update(wall_time_s=time.monotonic()-started,
                                sources_unchanged=sources() == plan["source_sha256"])
        study = write_verified_input(output / "study.json", study)
    return study


def write_report(output, study, verdict):
    """Small readable result with diagnostic pixels; no private world manifest."""
    output = Path(output)
    rows = []
    visual = study.get("visual") or {}
    from .starship_visual_observation import FrameStore
    store = FrameStore(output / "camera") if (output / "camera").is_dir() else None
    results = {r["request_id"]: r for r in visual.get("results", [])}
    for request in visual.get("requests", []):
        result = results.get(request["request_id"], {})
        diagnosis = (result.get("response") or {}).get("visual_diagnosis") or {}
        images = "".join('<figure><img width="320" src="data:image/png;base64,'+base64.b64encode(store.read(f)).decode("ascii")+'">'
                         + '<figcaption>'+html.escape(f["camera_id"])+f' · t={f["capture_sim_time_s"]:.2f}s</figcaption></figure>'
                         for f in request["manifest"]["frames"])
        rows.append('<section><h2>'+html.escape(request["evidence_kind"])+f' · t={request["snapshot_time_s"]:.2f}s</h2>'
                    +'<p>'+html.escape(json.dumps({"diagnosis": diagnosis, "accepted": result.get("accepted"),
                                                   "rejection": result.get("rejection")}, ensure_ascii=False))+'</p>'
                    +'<div class="frames">'+images+'</div></section>')
    body = '<!doctype html><html lang="ja"><meta charset="utf-8"><title>Starship visual fixture</title>'
    body += '<style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 20px;background:#101827;color:#eaf1ff}section{margin:32px 0}p,pre{overflow-wrap:anywhere;white-space:pre-wrap}.frames{display:flex;flex-wrap:wrap}figure{margin:8px}img{max-width:100%}</style>'
    body += '<h1>Starship：数値異常から画像観測へ</h1><p>固定応答を返すローカルHTTP fixture。画像は物理状態から描画した簡略カメラです。Jev推論・実機映像・実機飛行の証拠ではありません。</p>'
    body += '<pre>'+html.escape(json.dumps(verdict, ensure_ascii=False, indent=2))+'</pre>'+''.join(rows)+'</html>'
    replace_derived(output / "report.html", body.encode())
    write_manifest(output, study["run_id"])
