#!/usr/bin/env python3
"""Inherited subprocess + real loopback Gateway; synthetic state/provider only.

No flight, forecast, model, human approval or numerical policy is executed.
The scripted operator is an explicit smoke principal, never the host pump.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/"packages/missionos-core/src"))
from src.runtime import starship_restored_native_host as native  # noqa: E402
from src.runtime import starship_restored_contract as contract  # noqa: E402
from src.runtime.starship_inflight_runtime import ApprovalPipeReader, ApprovalPipeWriter, approval_pipe  # noqa: E402
from src.runtime.starship_restored_approval import RestoredApprovalBinding, RestoredApprovalWorker  # noqa: E402
from src.runtime.starship_restored_decision_pipe import RestoredDecisionClient  # noqa: E402
from src.runtime.starship_restored_integration import RestoredIntegrationPolicy  # noqa: E402
from src.runtime.starship_restored_return import RestoredReturnRequest  # noqa: E402
from scripts.smoke_starship_restored_approval import synthetic_origin  # noqa: E402


def _save(path, value):
    with Path(path).open("xb") as handle:
        handle.write(native._wire(value))


@contextmanager
def loopback_gateway(directory, *, clock=time.monotonic):
    """Actual Gateway lifecycle reusable by the opt-in outer parent runner."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = {"MISSIONOS_LLM_BACKEND":"off", "MISSIONOS_AGENT_RUNTIME_ADK_ENABLED":"0",
        "MISSIONOS_MISSION_ASSURANCE_ADK_ENABLED":"0", "GATEWAY_API_KEY":"", "GATEWAY_HOST":"127.0.0.1",
        "GATEWAY_PORT":str(port), "TASK_STORE_DB_PATH":str(directory/"tasks.db"),
        "MEMORY_DB_PATH":str(directory/"memory.db"), "AUDIT_LOG_PATH":str(directory/"audit.log")}
    with patch.dict(os.environ, config):
        from src.config.settings import reset_settings
        reset_settings()
        from src.gateway import server, starship_inflight_approval as routes
        import uvicorn
        service = routes.InflightApprovalService(clock=clock)
        with patch.object(routes, "_SERVICE", service):
            gateway = server.create_missionos_gateway()
            http = uvicorn.Server(uvicorn.Config(gateway.app, host="127.0.0.1", port=port, log_level="error", lifespan="off"))
            thread = threading.Thread(target=lambda:http.run(sockets=[sock]), daemon=True)
            thread.start()
            until = time.monotonic()+15.
            while not http.started and time.monotonic() < until:
                time.sleep(.01)
            try:
                native._require(http.started, "native_smoke_gateway_start_failed")
                yield service, f"http://127.0.0.1:{port}"
            finally:
                http.should_exit = True
                thread.join(timeout=10.)
                sock.close()
                reset_settings()


def operator_http(address, context, token, *, request_id=None, choice=None, body=None):
    """Explicit caller action; never invoked from RestoredNativeHost.tick."""
    from src.gateway.starship_inflight_approval import PREFIX
    base = PREFIX+f"/runs/{context['run_id']}/missions/{context['mission_id']}/vehicles/{context['vehicle_id']}"
    path = base+("/pending" if request_id is None else f"/requests/{request_id}"+("/"+choice if choice else ""))
    headers = {"Origin":address,"Referer":address+PREFIX,"Sec-Fetch-Site":"same-origin",
        "X-MissionOS-Operator":"starship-inflight-v1","Authorization":"Bearer "+token}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(address+path, data=None if body is None else native._wire(body), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5.) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


class FixedSmokeAgent:
    def __init__(self, *, active, clock):
        self.active, self.clock = active, clock
    def assess(self, request, *, provider, **_kwargs):
        native._require(self.active(), "native_smoke_inactive")
        return {"schema":"missionos.starship_restored_judgment.v1","request_id":request["request_id"],
            "request_sha256":native._hash(request),"provider":provider,"status":"valid","action":"continue_deployment",
            "summary":"Synthetic fixed judgment, no model invoked.","model_inference_invoked":False,
            "execution_permission_granted":False,"provider_receipt":{"status":"synthetic_fixed_response"}}


def _telemetry(job, origin, binding, state, sequence, revision=0, reservation=None):
    request = job["initial_request"] if reservation is None else reservation
    bound = binding.record()
    runtime = {"run_active":True,"simulation_time_s":state["time_s"],"restored_origin_sha256":bound["restored_origin_sha256"],
        "revision":revision,"scope":bound["initial_scope"] if revision == 0 else bound["registry"],
        "reservation_sha256":native._hash(request),"observation":{"state_sha256":native._hash(state),
            "simulation_time_s":state["time_s"],"wall_time_s":time.monotonic()}}
    return {"schema":native.TELEMETRY_SCHEMA,"context":job["context"],"plan_sha256":job["plan_sha256"],
        "source_sha256":native._hash(job["source_sha256"]),"sequence":sequence,"runtime_state":runtime,
        "actual_state":state,"phase":"orbital_coast","vehicle_sha256":native._hash(origin.record()["vehicle"]),
        "binding_sha256":native._hash(request),"return_controller_started":False,
        "integration_policy_sha256":native._hash(RestoredIntegrationPolicy(2).record()),"state_assigned":False}


def _worker(directory, fd_values, scenario):
    """Scripted worker uses inherited pipes; never imports a signing secret."""
    job = json.loads((directory/"job.json").read_bytes())
    profile = (directory/"profile.json").read_bytes()
    raw = (directory/"governance/closed-origin.json").read_bytes()
    origin = contract.ActualRestoredOrigin(sha256(raw).hexdigest(),raw,profile,job["source_sha256"])
    channels = [ApprovalPipeWriter(fd_values[0]),ApprovalPipeReader(fd_values[1]),ApprovalPipeWriter(fd_values[2]),
        ApprovalPipeReader(fd_values[3]),ApprovalPipeWriter(fd_values[4]),ApprovalPipeReader(fd_values[5]),ApprovalPipeWriter(fd_values[6])]
    decision = RestoredDecisionClient(context=job["context"],plan_sha256=job["plan_sha256"],source_sha256=native._hash(job["source_sha256"]),
        request_writer=channels[0],response_reader=channels[1])
    request = {"schema":"missionos.starship_restored_decision_request.v1","request_id":"synthetic-deploy",
        "phase":"deployment","scope_sha256":"a"*64,"time_s":origin.time_s,"deadline_s":origin.time_s+35.,
        "facts":{"time_s":origin.time_s,"released_count":0,"retained_count":26},"notice_summary":"Synthetic smoke only.",
        "allowed_actions":["continue_deployment","withhold_deployment"]}
    decision.begin(request)
    deadline, exchange = time.monotonic()+10., None
    while exchange is None and time.monotonic() < deadline:
        exchange = decision.poll(origin.time_s)
        time.sleep(.001)
    native._require(exchange is not None and exchange["status"] == "valid" and exchange["model_inference_invoked"] is False,
        "native_smoke_fixture_decision_missing")
    binding = RestoredApprovalBinding(origin,context=job["context"],plan_sha256=job["plan_sha256"],
        expected_source_sha256=job["source_sha256"],started_wall_s=time.monotonic())
    anchor = contract.RestoredCandidateSchedule(origin)
    ready = {"schema":native.READY_SCHEMA,"context":job["context"],"plan_sha256":job["plan_sha256"],
        "source_sha256":native._hash(job["source_sha256"]),"profile_sha256":contract.PROFILE_SHA256,
        "origin_file":"closed-origin.json","source_record_sha256":origin.source_record_sha256,"origin_sha256":native._hash(origin.record()),
        "binding_sha256":native._hash(binding.record()),"opportunity_anchor_sha256":native._hash(anchor.opportunity_anchor_record()),
        "initial_reservation_sha256":native._hash(job["initial_request"]),"integration_policy_sha256":native._hash(RestoredIntegrationPolicy(2).record()),
        "started_wall_s":binding.record()["started_wall_s"],"nonce":secrets.token_hex(32)}
    if scenario == "ready_hash":
        ready["origin_sha256"] = "f"*64
    _save(directory/"governance/origin-ready.json",ready)
    channels[2].send(ready)
    frame = _telemetry(job,origin,binding,origin.record()["state"],1)
    if scenario == "telemetry_spoof":
        frame["runtime_state"]["observation"]["state_sha256"] = "f"*64
    channels[6].send(frame)
    ack = None
    while ack is None and time.monotonic() < deadline:
        channels[2].flush()
        channels[6].flush()
        ack = channels[3].poll()
        time.sleep(.001)
    native._require(ack is not None and ack.get("ready_sha256") == native._hash(ready), "native_smoke_ack_missing")
    if scenario in ("ready_replay","telemetry_replay"):
        channels[2 if scenario == "ready_replay" else 6].send(ready if scenario == "ready_replay" else frame)
        time.sleep(2.)
        return
    state, updates, reservation = frame["runtime_state"], [], [job["initial_request"]]
    def command(action,current,consumption):
        actual = deepcopy(frame["actual_state"])
        changed = json.loads(origin.source_record_bytes())
        changed["final_state"],changed["inventory"]["observed_time_s"] = actual,actual["time_s"]
        changed["state_history"].append({"state":actual,"retained_count":26})
        encoded = native._wire(changed)
        _save(directory/"governance/reservation-change-origin.json",changed)
        current_origin = contract.ActualRestoredOrigin(sha256(encoded).hexdigest(),encoded,profile,job["source_sha256"])
        reservation[0] = RestoredReturnRequest(current_origin,contract.RestoredCandidateSchedule(current_origin,anchor=anchor),
            "next_orbit",profile,job["source_sha256"]).record()
        current.update(revision=1,scope=consumption["scope"],reservation_sha256=native._hash(reservation[0]))
        state.update(current)
        updates.append(action)
        return {"applied":True,"revision":1,"reservation_sha256":state["reservation_sha256"]}
    actor = RestoredApprovalWorker(binding,initial_reservation_sha256=native._hash(job["initial_request"]),
        request_writer=channels[4],grant_reader=channels[5],admission=lambda *_args:{"accepted":True,"candidate_sha256":job["candidate_sha256"],
            "synthetic_numerical_check_not_a_prediction":True},command=command)
    actor.request_change(request_id="synthetic-amendment",candidate_sha256=job["candidate_sha256"],state=state)
    while actor.status == "awaiting_operator" and time.monotonic() < deadline:
        actor.tick(state)
        time.sleep(.001)
    native._require(actor.status == "reservation_changed_waiting_observation", "native_smoke_scope_not_consumed")
    new_actual = deepcopy(frame["actual_state"])
    new_actual["time_s"] += .25
    frame = _telemetry(job,origin,binding,new_actual,2,revision=1,reservation=reservation[0])
    state.update(frame["runtime_state"])
    channels[6].send(frame)
    actor.confirm(state)
    _save(directory/"worker-result.json",{"synthetic_only":True,"decision_client":decision.record(),"approval":actor.record(),
        "updates":updates,"provider_calls":0,"plant_steps":0,"worker_environment_names":sorted(os.environ)})


def _source_manifest():
    paths = {*(ROOT/"src/runtime").glob("starship*.py"),*(ROOT/"src/intelligence").glob("starship*.py"),
        *(ROOT/name for name in contract.CRITICAL_SOURCES),Path(__file__).resolve(),
        ROOT/"scripts/smoke_starship_restored_approval.py", ROOT/"src/gateway/server.py",
        ROOT/"src/gateway/starship_inflight_approval.py", ROOT/"src/config/settings.py",
        *(ROOT/"packages/missionos-core/src").rglob("*.py")}
    return {path.relative_to(ROOT).as_posix():sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}


def run_smoke(output):
    output = Path(output).absolute()
    native._require(not output.exists() and not output.is_symlink(), "native_smoke_fresh_output_required")
    output.mkdir(parents=True)
    from src.runtime import starship_sixdof as dyn, starship_physics as env, starship_sixdof_mission as mission
    from src.intelligence.starship_restored_agent import RestoredAgent
    from src.intelligence.starship_m2_broker import DeadlineTransport
    def forbidden(*_args,**_kwargs):
        raise AssertionError("native host smoke forbids all plant integration and hosted models")
    checks, cases, sources = {}, [], _source_manifest()
    original, _ = synthetic_origin()
    raw = json.loads(original.source_record_bytes())
    raw["source_sha256"] = sources
    encoded = native._wire(raw)
    profile = (ROOT/"examples/spaceflight/starship-sixdof-profile.json").read_bytes()
    origin = contract.ActualRestoredOrigin(sha256(encoded).hexdigest(),encoded,profile,sources)
    initial = RestoredReturnRequest(origin,contract.RestoredCandidateSchedule(origin),"nominal",profile,sources)
    with ExitStack() as stack:
        for owner,name in ((dyn,"step"),(dyn,"_rhs"),(dyn,"_rk4"),(env,"step"),(mission,"advance_plant"),
            (RestoredAgent,"assess"),(DeadlineTransport,"_invoke")):
            stack.enter_context(patch.object(owner,name,forbidden))
        service,address = stack.enter_context(loopback_gateway(output/"gateway"))
        for scenario in ("success","ready_hash","telemetry_spoof","ready_replay","telemetry_replay"):
            directory = output/scenario
            (directory/"governance").mkdir(parents=True)
            (directory/"profile.json").write_bytes(profile)
            (directory/"governance/closed-origin.json").write_bytes(encoded)
            context = {"run_id":"native-smoke-"+scenario,"mission_id":"synthetic","vehicle_id":"ship"}
            plan = native._hash({"source":sources,"scenario":scenario,"synthetic_only":True})
            job = {"context":context,"plan_sha256":plan,"source_sha256":sources,"initial_request":initial.record(),
                "candidate_sha256":native._hash({"fixed_synthetic_proposal":True,"next_time_s":contract.RestoredCandidateSchedule(origin).record()["next_orbit_s"]})}
            _save(directory/"job.json",job)
            pairs = [approval_pipe() for _ in range(7)]
            worker_fds = [pairs[i][1 if i in (0,2,4,6) else 0].fd for i in range(7)]
            token, key = secrets.token_hex(24), secrets.token_bytes(32)
            active = [True]
            host = native.RestoredNativeHost(context=context,plan_sha256=plan,expected_source_sha256=sources,profile_raw_bytes=profile,
                integration_policy=RestoredIntegrationPolicy(2),mode="fixture",governance_dir=directory/"governance",
                ledger_path=directory/"operator.sqlite",signing_key=key,operator_token=token,operator_id="scripted-smoke-operator",
                decision_request_reader=pairs[0][0],decision_response_writer=pairs[1][1],ready_reader=pairs[2][0],ack_writer=pairs[3][1],
                approval_request_reader=pairs[4][0],approval_grant_writer=pairs[5][1],telemetry_reader=pairs[6][0],service=service,
                agent_factory=FixedSmokeAgent,active=lambda:active[0])
            child_env = {key:value for key,value in os.environ.items() if key in {"PATH","HOME","TMPDIR","LANG","LC_ALL"}}
            child_env.update(OPENBLAS_NUM_THREADS="1",OMP_NUM_THREADS="1")
            transcript, approved, rejection = [], False, None
            with (directory/"worker.log").open("xb") as log:
                child = subprocess.Popen([sys.executable,str(Path(__file__).resolve()),"--_worker",str(directory),
                    "--fds",json.dumps(worker_fds),"--scenario",scenario],cwd=ROOT,env=child_env,
                    pass_fds=worker_fds,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                until = time.monotonic()+20.
                try:
                    while child.poll() is None and time.monotonic() < until:
                        try:
                            host.tick()
                        except ValueError as error:
                            rejection = str(error)
                            break
                        if scenario == "success" and host.approval_host is not None and host.approval_host.bridge.request is not None and not approved:
                            status,pending = operator_http(address,context,token)
                            request = pending["request"]
                            body = {"request_sha256":request["sha256"],"observed_state_sha256":request["observed_state_sha256"],
                                "candidate_sha256":request["candidate_sha256"]}
                            transcript.append({"method":"GET","status":status,"response":pending})
                            status,response = operator_http(address,context,token,request_id=request["request_id"],choice="approve",body=body)
                            transcript.append({"method":"POST","status":status,"response":response})
                            approved = status == 200
                        time.sleep(.001)
                    if child.poll() is None:
                        child.terminate()
                        child.wait(timeout=5.)
                    active[0] = False
                    if rejection is None:
                        host.tick()
                    host_record = host.record()
                    _save(directory/"host-record.json",host_record)
                    _save(directory/"http.json",transcript)
                    if scenario == "success":
                        worker = json.loads((directory/"worker-result.json").read_bytes())
                        checks["real_inherited_decision_and_false_inference"] = worker["decision_client"]["results"][0]["model_inference_invoked"] is False
                        checks["real_http_candidate_bound_approval"] = approved and transcript[-1]["status"] == 200
                        checks["one_consumption_and_later_confirmation"] = worker["approval"]["status"] == "reservation_confirmed" and len(worker["updates"]) == 1
                        checks["no_keys_or_provider_credentials_to_worker"] = not any("KEY" in name or "TOKEN" in name or "CREDENTIAL" in name for name in worker["worker_environment_names"])
                        checks["host_preserved_worker_wall"] = all(0 <= row["received_wall_s"]-row["frame"]["runtime_state"]["observation"]["wall_time_s"] <= 5. for row in host_record["telemetry"])
                    else:
                        checks[scenario+"_rejected"] = rejection is not None and (host_record["handshake"]["ack"] is None
                            if scenario in ("ready_hash","telemetry_spoof") else host_record["handshake"]["ack"] is not None)
                    cases.append({"scenario":scenario,"returncode":child.returncode,"rejection":rejection,
                        "host_record_sha256":sha256((directory/"host-record.json").read_bytes()).hexdigest()})
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait()
                    host.close()
                    for pair in pairs:
                        for endpoint in pair:
                            endpoint.close()
    native._require(_source_manifest() == sources,"native_smoke_source_changed")
    result = {"schema":"missionos.starship_restored_native_host_smoke.v1","passed":all(checks.values()),"checks":checks,
        "cases":cases,"source_sha256":sources,"source_unchanged":True,"plant_steps":0,"model_calls":0,
        "synthetic_actual_state_only":True,"operator_is_scripted_smoke_principal":True,"physical_return_verified":False,
        "qualification_refreshed":False,"mission_authority_granted":False}
    _save(output/"summary.json",result)
    native._require(result["passed"],"native_smoke_failed")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir",type=Path)
    parser.add_argument("--run",action="store_true")
    parser.add_argument("--_worker",type=Path,help=argparse.SUPPRESS)
    parser.add_argument("--fds",help=argparse.SUPPRESS)
    parser.add_argument("--scenario",help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._worker is not None:
        native._require(not args.run and args.output_dir is None and args.fds is not None,
            "native_smoke_worker_flags_invalid")
        from src.runtime import starship_sixdof as dyn, starship_physics as env, starship_sixdof_mission as mission
        from src.intelligence.starship_restored_agent import RestoredAgent
        from src.intelligence.starship_m2_broker import DeadlineTransport
        def forbidden(*_args, **_kwargs):
            raise AssertionError("Synthetic inherited worker forbids plant and native providers")
        with ExitStack() as stack:
            for owner,name in ((dyn,"step"),(dyn,"_rk4"),(dyn,"_rhs"),(env,"step"),
                (mission,"advance_plant"),(RestoredAgent,"assess"),(DeadlineTransport,"_invoke")):
                stack.enter_context(patch.object(owner,name,forbidden))
            return _worker(args._worker,json.loads(args.fds),args.scenario)
    if not args.run or args.output_dir is None or args.fds is not None:
        parser.error("explicit --run and fresh --output-dir required")
    try:
        result = run_smoke(args.output_dir)
    except BaseException as error:
        if args.output_dir.is_dir() and not (args.output_dir/"stopped.json").exists():
            _save(args.output_dir/"stopped.json", {"schema":"missionos.starship_restored_native_host_smoke_stop.v1",
                "exception_type":type(error).__name__,"reason":str(error)[:200],"automatic_retry":False,
                "synthetic_only":True,"plant_steps":0,"model_calls":0})
        raise
    print(json.dumps({"passed":result["passed"],"plant_steps":0,"model_calls":0}))


if __name__ == "__main__":
    main()
