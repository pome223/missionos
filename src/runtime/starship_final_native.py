"""Maintained opt-in composition: one ground state through guarded return.

No provider or approval key lives here. The old pre-dispatch owner terminates
at its sealed actual-state handoff. A new sensor continuation consumes that
same state/actuator/inventory once; it never creates a completed legacy-v1
flight or borrows an earlier flight's outcome.
"""
from copy import deepcopy
import gzip
from pathlib import Path
import time

from . import starship_final_shared as shared
from .starship_final_timing import MissionWallBudget
from .starship_final_plan import verification_inputs
from .starship_final_verifier import SCHEMA, CLAIM_LIMITS, execution_binding
from .starship_final_protocol import FinalObservationClient, request as rpc_request
from .starship_final_visual import FinalVisualObserver, FinalVisualPolicy, PENDING
from .starship_final_prefix_supervision import FinalPrefixSupervision, FinalPrefixSupervisionPolicy
from .starship_final_handoff import FinalActualReturnHandoffPolicy
from .starship_final_faults import FinalFaultHarness
from .starship_final_sensor_execution import FinalSensorExecution, FinalSensorExecutionPolicy, numerical_backend
from .starship_final_tasks import NumericalFuture
from .starship_restored_decision_pipe import RestoredDecisionClient
from .starship_restored_deployment import RestoredDeploymentJudgments
from .starship_restored_prefix import RestoredPrefixRuntime, restored_prefix_mission
from .starship_restored_coordinator import RestoredReturnCoordinator
from .starship_restored_operations import RegisteredRestoredOperations
from .starship_restored_integration import RestoredIntegrationPolicy, restored_integration


class RemoteDecisionFuture:
    """One inherited request; polling cannot renew its original epoch."""
    def __init__(self, client, payload, *, deadline_monotonic, simulation_clock):
        self.client, self.clock, self.result_value = client, simulation_clock, None
        self.payload = deepcopy(payload)
        rpc = rpc_request(payload["request_id"], "sensor", payload["issued_at_s"], payload["deadline_s"], payload)
        start = deadline_monotonic-(payload["deadline_s"]-payload["issued_at_s"])
        client.begin(rpc, started_wall_s=start)
        self.cancelled = False

    def done(self):
        if not self.cancelled and self.result_value is None:
            self.result_value = self.client.poll(self.clock())
        return self.cancelled or self.result_value is not None

    def result(self):
        shared.require(not self.cancelled and self.done(), "final_remote_future_unavailable")
        return deepcopy(self.result_value)

    def cancel(self):
        # No replacement request and no invented simulation clock to retire it.
        # The host may finish late; the retained client/host transcripts expose
        # that result, which this consumer never adopts.
        self.cancelled = True
        return False


def visual_rpc_request(observer, envelope):
    """Project the actual pending image while retaining its original epoch."""
    shared.require(type(observer) is FinalVisualObserver
        and observer.record()["pending_request"] == envelope,
        "final_visual_rpc_not_actual_pending_observation")
    value = envelope["request"]
    return rpc_request(value["request_id"], "visual", observer.original_epoch_s,
        value["deadline_s"], envelope)


def run_final_native_worker(plan, *, output_dir, channels, clock=time.monotonic, sleep=time.sleep, wall_clock=None):
    inputs = verification_inputs(plan)
    source, context, mode = inputs["source_sha256"], inputs["context"], inputs["mode"]
    profile = inputs["profile"]
    shared.check_manifest(source)
    shared.require(shared.environment() == inputs["backend"], "final_worker_backend_changed")
    output = Path(output_dir)
    shared.require(output.is_dir() and not output.is_symlink()
        and not (output/"prefix.json.gz").exists() and not (output/"governance").exists(),
        "final_worker_exclusive_output")
    required = {"decision_request_writer", "decision_response_reader", "ready_writer", "ready_reader",
        "approval_request_writer", "approval_grant_reader", "telemetry_writer", "observation_request_writer", "observation_response_reader"}
    shared.require(set(channels) == required, "final_worker_nine_channels_required")
    client = RestoredDecisionClient(context=context, plan_sha256=inputs["plan_sha256"],
        source_sha256=shared.digest(source), request_writer=channels["decision_request_writer"],
        response_reader=channels["decision_response_reader"], return_review_policy=inputs["return_review_policy"])
    rpc = FinalObservationClient(channels["observation_request_writer"], channels["observation_response_reader"],
        plan_sha256=inputs["plan_sha256"], source_sha256=shared.digest(source), clock=clock)
    profile_raw = shared.relative(shared.PROFILE).read_bytes()
    settings = shared.ascent_settings(plan["configuration"]["ascent_settings"])
    controls = shared.booster_control(inputs["control_settings"])
    sites, goal = shared.sites_and_goal(profile)
    operations = RegisteredRestoredOperations(inputs["operations"])
    integration = RestoredIntegrationPolicy.from_record(inputs["integration_policy"])
    tasks, coordinator, sensor, harness = [], None, None, None
    timing = MissionWallBudget(inputs["bounds"]["per_mission_wall_s"], clock=clock, wall_clock=wall_clock,
        reason="final_worker_mission_wall_budget")

    def budget():
        timing.check()

    def visual_decide(envelope):
        value = envelope["request"]
        if rpc.pending is None:
            rpc.begin(visual_rpc_request(visual, envelope),
                started_wall_s=envelope["started_wall_s"])
        shared.require(rpc.pending["frame"]["request"]["request_id"] == value["request_id"], "final_visual_rpc_foreign_request")
        reply = rpc.poll(value["snapshot_time_s"] if visual._clock is None else visual._clock)
        return PENDING if reply is None else reply

    visual = FinalVisualObserver(profile, output/"camera", run_id=plan["run_id"], plan_sha256=inputs["plan_sha256"],
        source_sha256=source, mode=mode, decide=visual_decide,
        policy=FinalVisualPolicy(**plan["configuration"]["visual_policy"]), clock=clock)
    supervision = FinalPrefixSupervision(profile, run_id=plan["run_id"], plan_sha256=inputs["plan_sha256"],
        source_sha256=source, scope_sha256=shared.digest({"context": context, "phase": "prefix", "plan": inputs["plan_sha256"]}),
        client=rpc, mode=mode, policy=FinalPrefixSupervisionPolicy(**plan["configuration"]["supervision_policy"]), clock=clock)
    deployment = RestoredDeploymentJudgments(client, scope_sha256=shared.digest({"context": context,
        "plan_sha256": inputs["plan_sha256"], "source_sha256": source, "phase": "deployment"}),
        notice=operations.deployment_notice, maximum_existing_step_s=max(profile["integration"]["powered_dt_s"],
            profile["integration"]["coast_dt_s"]), clock=clock, sleep=sleep)

    def on_tick(tick):
        budget()
        if rpc.pending is not None:
            frame = rpc.pending["frame"]
            elapsed = min(max(0., tick.time_s-frame["request"]["time_s"]),
                frame["request"]["deadline_s"]-frame["request"]["time_s"])
            while clock()-frame["started_wall_s"] < elapsed:
                budget()
                rpc.writer.flush()
                sleep(min(.01, elapsed-(clock()-frame["started_wall_s"])))

    try:
        with restored_integration(integration) as numerical:
            runtime = RestoredPrefixRuntime(profile_raw, source, settings, controls, 7500., deployment,
                final_visual_observer=visual, final_prefix_supervision=supervision)
            prefix = restored_prefix_mission(profile, runtime, return_sites=sites, splashdown_goal=goal, on_tick=on_tick)
            raw = shared.wire(prefix)
            with (output/"prefix.json.gz").open("xb") as stream:
                stream.write(gzip.compress(raw, mtime=0))
            shared.require(gzip.decompress((output/"prefix.json.gz").read_bytes()) == raw,
                "final_prefix_restore_mismatch")
            prefix = shared.parse(raw)
            deployment_record = deployment.finalize_prefix(prefix)
            # Incomplete physical prefixes stay saved negatives. Never splice a
            # previously successful launch to make this record look complete.
            if prefix["restored_prefix"]["closed"] is True:
                actual_origin = runtime.origin()
                operation_tool = operations.bind(actual_origin)
                coordinator = RestoredReturnCoordinator(actual_origin, profile_raw_bytes=profile_raw,
                    expected_source_sha256=source, integration_policy=integration, context=context,
                    plan_sha256=inputs["plan_sha256"], output_dir=output/"governance", decision_client=client,
                    ready_writer=channels["ready_writer"], ready_reader=channels["ready_reader"],
                    approval_request_writer=channels["approval_request_writer"], approval_grant_reader=channels["approval_grant_reader"],
                    telemetry_writer=channels["telemetry_writer"], sites=operations.sites, notice_resources=operation_tool,
                    notice_generations=operations.notice_generations, deployment_record=deployment_record,
                    notice_summary=operation_tool.notice_summary, return_context_projection=inputs["return_context_projection"],
                    return_review_policy=inputs["return_review_policy"],
                    final_handoff_policy=FinalActualReturnHandoffPolicy(inputs["plan_sha256"], source))
                coordinator.start()
                while coordinator.result is None and coordinator.final_handoff is None:
                    budget()
                    coordinator.tick()
                coordinator.close()  # Reap copied planning workers before new suffix tasks.
                if coordinator.final_handoff is not None:
                    harness = FinalFaultHarness(inputs["fault_selection"],
                        expected_selection_sha256=inputs["fault_selection"]["selection_sha256"])

                    def decide(payload, *, deadline_monotonic):
                        return RemoteDecisionFuture(rpc, payload, deadline_monotonic=deadline_monotonic,
                            simulation_clock=lambda: sensor.state.time_s)

                    def submit(kind, payload, *, deadline_monotonic):
                        future = NumericalFuture(output/("numerical-"+str(len(tasks))+"-"+kind), kind, payload,
                            deadline_monotonic=deadline_monotonic, clock=clock)
                        tasks.append(future)
                        return future

                    sensor = FinalSensorExecution(coordinator.final_handoff, harness, profile,
                        run_id=plan["run_id"], source_sha256=source, backend=numerical_backend(inputs["backend"]),
                        policy=FinalSensorExecutionPolicy(**inputs["sensor_policy"]), integration_policy=integration, decide=decide,
                        forecast=lambda payload, **kw: submit("forecast", payload, **kw),
                        guard=lambda payload, **kw: submit("guard", payload, **kw), clock=clock)
                    while sensor.termination is None:
                        budget()
                        if sensor.step_once()["waiting"]:
                            sleep(.01)
        timing.finish()
        return {"schema": SCHEMA, "execution": execution_binding(inputs), "prefix": prefix,
            "governance": None if coordinator is None else coordinator.record(),
            "handoff": None if coordinator is None or coordinator.final_handoff is None else coordinator.final_handoff.record(),
            "fault_harness": None if harness is None else harness.record(),
            "sensor_return": None if sensor is None else sensor.result(),
            "supplemental": {"supervision": supervision.record(), "visual": visual.record(), "client": rpc.record(),
                "numerical_tasks": [task.record() for task in tasks]},
            "numerical_integration": numerical.record(), "claim_limits": deepcopy(CLAIM_LIMITS)}
    finally:
        try:
            shared.save(output/"worker-timing.json", {"component": "native_worker_before_serialization",
                "plan_sha256": inputs["plan_sha256"], "source_sha256": shared.digest(source), "timing": timing.record()})
        finally:
            try:
                if coordinator is not None:
                    coordinator.close()
            finally:
                for task in tasks:
                    if task.running:
                        task.cancel()
