"""Credential-free M1 actor. AI decisions, predictions and execution are separate.

Launch/deployment use the existing production loop. Its exact final state is
continued with the shared ReturnController and finite plant stepper. Predictions
never replace execution state. The simulation clock advances during every tool
or model wait; between decisions the numerical simulation may run accelerated.
"""

from copy import deepcopy
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import time
import signal

from . import starship_sixdof as dyn, starship_physics as env
from .starship_sixdof_mission import simulate, vehicle, point_state, _sample
from .starship_ship_return import ReturnController, control_interval, record_sample, advance_plant
from .starship_return_prediction import forecast, opportunity_window
from .starship_replanning import (
    digest,
    estimate,
    uncertainty_origins,
    notices,
    resources,
    source_hashes,
)
from .starship_replanning_verifier import admit, metrics, wait_reasons, fresh_match
from .starship_mission_director import publish
from .starship_artifacts import write_verified_checkpoint, write_verified_input
from .starship_orbital_window import (
    CONFIG as WINDOW_CONFIG, decision_window, reserved_decision_s, prepared_reasoning_eligible,
)

HANDOFF_TIME_S = 1000.7000000000407


def prediction_job(job):
    forecast_timeout_s = 180.
    if len(job) == 8:
        *job, forecast_timeout_s = job
        if type(forecast_timeout_s) not in (int, float) or forecast_timeout_s != 300.:
            raise ValueError("invalid_registered_forecast_resource_profile")
    compact_artifacts = False
    if len(job) == 7:
        *job, compact_artifacts = job
        if type(compact_artifacts) is not bool:
            raise ValueError("invalid_forecast_artifact_mode")
    origin, profile, when, scheduled, destination, integration_scale = job
    profile = deepcopy(profile)
    for field in ("powered_dt_s", "coast_dt_s"):
        profile["integration"][field] *= integration_scale

    def expired(_signum, _frame):
        raise TimeoutError("m1_forecast_wall_deadline")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, forecast_timeout_s)
    try:
        result = forecast(
            origin,
            profile,
            return_time_s=when,
            duration_s=10000.0,
            window=opportunity_window(scheduled),
        )
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
    path = Path(destination)
    if compact_artifacts:
        # The checkpoint writer verifies the saved compressed bytes and returns
        # their decoded JSON. Keep prediction identity tied to the exact same
        # uncompressed serialization used by legacy forecast artifacts.
        result = write_verified_checkpoint(path, result)
        prediction_sha256 = json.loads(path.read_bytes())["json_sha256"]
    else:
        result = write_verified_input(path, result)
        prediction_sha256 = sha256(path.read_bytes()).hexdigest()
    compact = {
        "origin": origin,
        "integration_scale": integration_scale,
        "final_state": result["final_state"],
        "outcome": result["outcome"],
        "return_time_s": when,
        "wall_time_s": result["wall_time_s"],
        "prediction_sha256": prediction_sha256,
        "coast": [
            {
                k: s[k]
                for k in (
                    "time_s",
                    "r_eci_m",
                    "v_eci_mps",
                    "q_body_to_eci",
                    "omega_body_rad_s",
                    "propellant_kg",
                )
            }
            for s in result["samples"]
            if s["phase"] == "orbital_coast"
        ],
    }
    return compact


class ReplanningExecutor:
    def __init__(
        self, profile, envelope, case, output, *, mailbox=None, run_id="standalone", deployment=None,
        composition=None
    ):
        self.p, self.envelope, self.case = profile, envelope, case
        if "orbital_planning_window" in envelope:
            reserved_decision_s(envelope)
            if not 0 < profile["integration"]["coast_dt_s"] <= WINDOW_CONFIG["coast_step_bound_s"]:
                raise ValueError("orbital_planning_coast_step_outside_contract")
        self.output, self.mailbox, self.run_id = (
            Path(output),
            Path(mailbox) if mailbox else None,
            run_id,
        )
        self.sources = source_hashes()
        self.deployment = deepcopy(deployment)
        self.deployment_binding = None
        self.retained_count = 0
        self.compact_artifacts = False
        if composition is not None:
            from .starship_unified_contract import validate_composition
            composition = validate_composition(composition)
            if (deployment is None or deployment["authority"] != composition["authority"]["m2_authority"]
                    or deployment.get("visual_authority") != composition["visual_authority"]
                    or envelope != composition["return_authority"] or envelope["mode"] != "fixture"):
                raise ValueError("unified_return_resource_authority_binding")
        if deployment is None:
            self.launch = simulate(
                profile, return_policy="trimmed_state_terminal_v4", duration_s=HANDOFF_TIME_S
            )
            if (
                self.launch["outcome"]["payload_released_count"] != 26
                or self.launch["samples"][-1]["phase"] != "orbital_coast"
            ):
                raise ValueError("m1_post_deployment_handoff_unavailable")
        else:
            from .starship_m2_contract import validate_deployment_origin

            context = validate_deployment_origin(
                profile, deployment["authority"], deployment["handoff"], deployment["launch"],
                visual_authority=deployment.get("visual_authority"),
            )
            if (
                envelope != (composition["return_authority"] if composition is not None
                             else deployment["authority"]["return_authority"])
                or case != deployment["authority"]["return_case"]
            ):
                raise ValueError("m2_return_authority_mismatch")
            # Only a validated visual deployment enables compact duplicates.
            # Neither a profile field nor an arbitrary caller flag grants it.
            self.compact_artifacts = deployment.get("visual_authority") is not None
            self.launch = deepcopy(deployment["launch"])
            self.retained_count = context["retained_count"]
            self.deployment_binding = {
                key: context[key]
                for key in (
                    "handoff_sha256",
                    "authority_sha256",
                    "profile_sha256",
                    "generation",
                    "released_count",
                    "retained_count",
                )
            }
            # An exact execution state resumes here. Never install predicted state.
            save_checkpoint = write_verified_checkpoint if self.compact_artifacts else write_verified_input
            save_checkpoint(self.output / "deployment-handoff.json", deployment)
        save_checkpoint = write_verified_checkpoint if self.compact_artifacts else write_verified_input
        save_checkpoint(self.output / "launch-checkpoint.json", self.launch)
        self.s = dyn.state_from_dict(self.launch["final_state"])
        self.origin = asdict(self.s)
        cutoff = next(
            e["time_s"] for e in self.launch["events"] if e["event"] == "orbit_cutoff_command"
        )
        self.scheduled = cutoff + profile["guidance"]["coast_before_return_s"]
        self.v = vehicle(profile, payload_count=self.retained_count)
        observed = self.observation()
        period = env.orbital_elements(point_state(dyn.state_from_dict(observed["state"])))[
            "period_s"
        ]
        self.times = {"nominal": self.scheduled, "next_orbit": self.scheduled + period}
        # Freeze before any forecast. Absolute times are never extended by replanning.
        write_verified_input(
            self.output / "opportunities.json",
            {
                "return_times_s": self.times,
                "original_scheduled_s": self.scheduled,
                "original_deadline_s": self.scheduled + 6000.0,
                "origin": observed,
            },
        )
        self.controller = ReturnController(
            "trimmed_state_terminal_v4",
            1e9,
            profile["payload"]["count"] - self.retained_count,
            allocation_enabled=True,
        )
        self.samples, self.events, self.decisions, self.pending, self.revisions, self.candidates = (
            [],
            [],
            [],
            [],
            [],
            {},
        )
        self.next_sample, self.forecast_count, self.revision, self.selected = (
            self.s.time_s,
            0,
            0,
            None,
        )
        self.dispatch, self.contact, self.termination = None, None, "return_unresolved"
        self.service_failed = False
        self.ground_observations = []
        self.comparison_preparations = []
        self.comparison_generations = set()
        self.comparison_context = None
        self.comparison_last_generation = None
        self.monitor = None
        if envelope.get("event_supervision"):
            from .starship_replanning_events import EventMonitor

            self.monitor = EventMonitor(envelope, self.times)
            self.monitor.poll(self.observation(), notices(self.case, self.s.time_s), self.selected)

    def event(self, name, detail="", **fields):
        self.events.append({"time_s": self.s.time_s, "event": name, "detail": detail, **fields})

    def step(self):
        if (
            self.controller.phase == "orbital_coast"
            and self.controller.return_time_s > self.scheduled + self.envelope["maximum_delay_s"]
            and self.s.time_s + self.p["integration"]["coast_dt_s"]
            > self.scheduled + self.envelope["maximum_delay_s"] + 1e-7
        ):
            raise ValueError("bounded_coast_expired_unresolved")
        o = dyn.observe(self.s, self.v)
        altitude = o["altitude_m"]
        step = self.controller.update(self.s, self.v, self.p, self.samples, self.event)
        self.s = step.state
        if step.transition_only:
            return
        command, diagnostics = self.controller.command(step, self.v, self.p, altitude)
        self.next_sample = record_sample(
            self.s,
            self.v,
            self.controller.phase,
            command,
            diagnostics,
            self.samples,
            self.next_sample,
            self.p,
        )
        if env.norm(self.s.omega_body_rad_s) > 5:
            raise ValueError("angular_rate_envelope_exceeded")
        self.s, receipt = advance_plant(
            self.s,
            self.v,
            command,
            control_interval(self.p, step.throttle, altitude),
            self.p["geometry"]["ship_length_m"],
            self.p["geometry"]["radius_m"],
            altitude,
            o["air_speed_mps"],
        )
        if getattr(self, "monitor", None) is not None and self.controller.phase == "orbital_coast":
            self.monitor.poll(self.observation(), notices(self.case, self.s.time_s), self.selected)
        if receipt and receipt["contact"]:
            self.contact = receipt
            self.termination = (
                "low_speed_surface_contact"
                if receipt["surface_relative_speed_mps"] <= 5
                else "surface_impact"
            )
            self.event(self.termination)

    def coast_to(self, when):
        if when > self.scheduled + self.envelope["maximum_delay_s"]:
            raise ValueError("original_return_deadline_exceeded")
        while self.s.time_s < when - 1e-7:
            self.step()
            if getattr(self, "monitor", None) is not None and self.monitor.pending:
                break

    def wait(self, label, poll, limit, *, cutoff_time_s=None):
        start_wall, start_sim = time.monotonic(), self.s.time_s
        value = None
        cutoff_reached = False
        coast_dt = self.p["integration"]["coast_dt_s"]
        generation = self.monitor.generation if getattr(self, "monitor", None) else None
        forecast_material = (
            self.comparison_generation(notices(self.case, self.s.time_s))
            if label == "return_forecasts" and self.envelope.get("comparison_preparation")
            else None
        )

        def interrupts_forecast(event):
            if event["kind"] == "candidate_deadline":
                return False
            if forecast_material is not None and event["kind"] == "notice_updated":
                delivered = self.monitor.observations[event["observation_index"]]["notice"]
                return self.comparison_generation(delivered) != forecast_material
            return True

        while True:
            elapsed = time.monotonic() - start_wall
            while self.s.time_s - start_sim < elapsed:
                if cutoff_time_s is not None and self.s.time_s + coast_dt > cutoff_time_s + 1e-7:
                    cutoff_reached = True
                    break
                self.step()
            if cutoff_time_s is not None and self.s.time_s + coast_dt > cutoff_time_s + 1e-7:
                # Cancel while the finite plant step still leads wall time;
                # waiting until another step is needed would freeze the clock
                # for the fractional remainder before this absolute cutoff.
                cutoff_reached = True
            if cutoff_reached or elapsed >= limit:
                break
            if generation is not None and self.monitor.generation != generation:
                # A deadline is already being addressed by an in-flight forecast.
                # New facts still cancel it immediately and invalidate its context.
                changed = [e for e in self.monitor.pending if e["generation"] > generation]
                if label != "return_forecasts" or any(interrupts_forecast(e) for e in changed):
                    break
            value = poll()
            if value is not None:
                break
            time.sleep(0.01)
        # A distinct measured state confirms even a very fast response.
        if self.s.time_s == start_sim:
            if cutoff_time_s is None or self.s.time_s + coast_dt <= cutoff_time_s + 1e-7:
                self.step()
            else:
                cutoff_reached = True
        wall = time.monotonic() - start_wall
        while self.s.time_s - start_sim < wall:
            if cutoff_time_s is not None and self.s.time_s + coast_dt > cutoff_time_s + 1e-7:
                cutoff_reached = True
                break
            self.step()
        if cutoff_reached:
            value = None
        if (
            generation is not None
            and self.monitor.generation != generation
            and label == "return_forecasts"
        ):
            changed = [e for e in self.monitor.pending if e["generation"] > generation]
            if any(interrupts_forecast(e) for e in changed):
                value = None
        self.pending.append(
            {
                "operation": label,
                "start_time_s": start_sim,
                "end_time_s": self.s.time_s,
                "wall_time_s": wall,
                "expired": value is None,
                **(
                    {"cutoff_time_s": cutoff_time_s, "cutoff_reached": cutoff_reached}
                    if cutoff_time_s is not None
                    else {}
                ),
            }
        )
        return value

    def observation(self):
        observed = estimate(self.s)
        observed["retained_count"] = getattr(self, "retained_count", 0)
        return observed

    def admit_candidate(self, candidate, notice, observation=None):
        return admit(
            candidate,
            self.envelope,
            self.observation() if observation is None else observation,
            notice,
            self.scheduled,
            expected_deployment_binding=getattr(self, "deployment_binding", None),
        )

    def checks(self, notice):
        observed = self.observation()
        return {
            name: self.admit_candidate(c, notice, observed) for name, c in self.candidates.items()
        }

    def fixture_response(self, request):
        from src.intelligence.starship_replanning import fixture_decision

        return fixture_decision(request, self.envelope)

    def decision_context(self):
        return {}

    def constrain_decision_request(self, request):
        return request

    def next_monitor_time(self, due):
        return due

    def dispatch_check(self, name, notice, observation):
        return self.admit_candidate(self.candidates[name], notice, observation)

    def choose(self, stage, allowed):
        from .starship_call_limits import has_call_slot

        policy = self.envelope.get("provider_budget_policy")
        if "provider_budget_policy" in self.envelope and (
            policy != "independent_provider_reservations_v1"
            or "orbital_planning_window" not in self.envelope
        ):
            raise ValueError("invalid_provider_budget_policy")
        if self.envelope.get("maximum_llm_calls") is None and policy is None:
            from .starship_call_limits import canonical_uncapped_jev_escalations
            if not canonical_uncapped_jev_escalations(self.envelope):
                raise ValueError("invalid_provider_budget_policy")
        budget_exhausted = (
            sum(
                r["request_disposition"] in ("submitted", "synthetic_outage")
                for r in self.decisions
            )
            >= self.envelope["maximum_jev_calls"]
        )
        notice = notices(self.case, self.s.time_s)
        request_observation = self.observation()
        triggers = self.monitor.drain() if getattr(self, "monitor", None) else []
        checks = self.checks(notice)
        # Preserve a still-admissible booking inside its commitment interval.
        # The fresh notice can revoke it; an unrelated update cannot consume the
        # remaining burn window waiting for another model response.
        coast_dt = self.p["integration"]["coast_dt_s"]
        horizon = self.envelope["decision_timeout_s"] + coast_dt
        deadline_fallback = bool(
            getattr(self, "monitor", None) is not None
            and self.envelope["event_supervision"].get("commitment_response")
            == "preserve_currently_admissible_booking_without_model_wait"
            and self.selected in checks
            and checks[self.selected]["accepted"]
            and -min(coast_dt, 2.001) <= self.times[self.selected] - self.s.time_s <= horizon
        )
        public = {
            name: {
                "candidate_sha256": digest(c),
                "return_time_s": c["return_time_s"],
                "predicted_contacts": [metrics(t) for t in c["trials"]],
                "constraint_rejections": checks[name]["reasons"],
                "areas": checks[name]["areas"],
            }
            for name, c in self.candidates.items()
        }
        if self.envelope.get("comparison_preparation"):
            from .starship_replanning_comparison import candidate_material

            for name, candidate in self.candidates.items():
                public[name]["comparison_material"] = candidate_material(
                    candidate, self.envelope, notice, self.scheduled
                )
        request = {
            "schema": "missionos.m1_decision.v1",
            "request_id": f"{self.run_id}-{len(self.decisions)}",
            "envelope_sha256": digest(self.envelope),
            "revision": self.revision,
            "time_s": self.s.time_s,
            "deadline_s": self.s.time_s + self.envelope["decision_timeout_s"],
            "stage": stage,
            "notice": notice,
            "resources": resources(self.envelope, self.s.time_s, self.scheduled),
            "candidates": public,
            "selected": self.selected,
            "allowed_actions": allowed,
        }
        request.update(self.decision_context())
        if self.envelope.get("comparison_preparation"):
            request["comparison_context"] = deepcopy(getattr(self, "comparison_context", None))
        request = self.constrain_decision_request(request)
        allowed = request["allowed_actions"]
        if "orbital_planning_window" in self.envelope:
            request["planning_window"] = decision_window(self.envelope, request)
            request["deadline_s"] = self.s.time_s + request["planning_window"]["duration_s"]
        provider_budget = None
        if policy == "independent_provider_reservations_v1":
            reserved = [r for r in self.decisions
                        if r["request_disposition"] in ("submitted", "synthetic_outage")]
            reasoned = {
                r["provider_budget"]["generation_sha256"] for r in reserved
                if (r.get("provider_budget") or {}).get("provider") == "deepseek"
            }
            provider = ("deepseek" if prepared_reasoning_eligible(
                self.envelope, request, reasoned) else "jev")
            used = sum((r.get("provider_budget") or {}).get("provider", "jev") == provider
                       for r in reserved)
            maximum = self.envelope["maximum_llm_calls" if provider == "deepseek" else "maximum_jev_calls"]
            provider_budget = {
                "provider": provider, "used_slots": used, "maximum_slots": maximum,
                "generation_sha256": (request["comparison_context"]["generation_sha256"]
                                      if provider == "deepseek" else None),
                "previous_reservations_sha256": digest([
                    {"request_id": r["request"]["request_id"],
                     "request_sha256": digest(r["request"])} for r in reserved
                ]),
            }
            # The response and host attempt hashes cover this reservation too.
            # A missed earlier request cannot let the host upgrade this route.
            request["provider_budget"] = deepcopy(provider_budget)
            budget_exhausted = not has_call_slot(used, maximum)
        if deadline_fallback:
            request["commitment_guard"] = {
                "candidate_id": self.selected,
                "candidate_sha256": digest(self.candidates[self.selected]),
                "return_time_s": self.times[self.selected],
                "guard_horizon_s": horizon,
            }
        if getattr(self, "monitor", None) is not None:
            request.update(
                context_generation=self.monitor.generation,
                trigger_event_ids=[e["id"] for e in triggers],
            )
        if (self.case == "decision_timeout" and stage == "updated_recovery_status") or (
            self.case in ("event_timeout", "comparison_timeout") and notice["sequence"] >= 2
        ):
            self.service_failed = True
        timeout_injected = self.service_failed
        if self.mailbox and not timeout_injected and not budget_exhausted and not deadline_fallback:
            publish(self.mailbox / "request.json", request)

        def poll():
            if timeout_injected:
                return None
            if not self.mailbox:
                return self.fixture_response(request)
            try:
                response = json.loads((self.mailbox / "response.json").read_text())
                if response.get("request_id") == request["request_id"] and response.get(
                    "request_sha256"
                ) == digest(request):
                    return response
            except (OSError, ValueError, TypeError):
                pass
            return None

        if deadline_fallback:
            # This is an immediate checked fallback, not a model request or a
            # later measurement. Physical execution and its observations follow.
            response = None
        elif budget_exhausted:
            # A fresh decision identity binds the fallback even when no model
            # call is available. Never reuse the preceding accepted decision.
            self.step()
            response = None
        else:
            response = self.wait("model:" + stage, poll, request["deadline_s"] - request["time_s"])
        if self.mailbox and "orbital_planning_window" in self.envelope:
            # Retire this request before starting another observation/forecast.
            # A superseded provider cannot block the next short decision window.
            publish(
                self.mailbox / "cancelled.json",
                {
                    "request_id": request["request_id"],
                    "request_sha256": digest(request),
                },
            )
        action = response.get("action") if response else None
        if action not in allowed or self.s.time_s > request["deadline_s"]:
            action = None
        stale = (
            getattr(self, "monitor", None) is not None
            and self.monitor.generation != request["context_generation"]
        )
        if stale:
            action = None
        record = {
            "request": request,
            "response": response,
            "accepted_action": action,
            "later_time_s": self.s.time_s,
            "later_observation": self.observation(),
            "synthetic_timeout_injected": timeout_injected,
            "request_disposition": (
                "deadline_fallback"
                if deadline_fallback
                else "budget_exhausted"
                if budget_exhausted
                else "synthetic_outage"
                if timeout_injected
                else "submitted"
            ),
            "fallback": action is None and not stale,
        }
        if provider_budget is not None:
            record["provider_budget"] = provider_budget
        if self.envelope.get("comparison_preparation"):
            record["request_observation"] = request_observation
        if getattr(self, "monitor", None) is not None:
            record.update(later_generation=self.monitor.generation, interrupted_by_events=stale)
        self.decisions.append(record)
        self.event("m1_decision", stage, action=action, request_id=request["request_id"])
        return action

    def calculate(self, names, *, budget_s=None, cutoff_time_s=None, synthetic_failure=False):
        bounded = budget_s is not None and bool(self.envelope.get("comparison_preparation"))
        if bounded:
            start_sim, start_wall = self.s.time_s, time.monotonic()
            budget_s = min(self.envelope["batch_timeout_s"], budget_s)
            cutoff_time_s = min(
                cutoff_time_s if cutoff_time_s is not None else float("inf"), start_sim+budget_s
            )
        if getattr(self, "deployment_binding", None) is not None and "next_orbit" in names:
            from .starship_return_time_search import search

            # Optional screening cannot veto the original opportunities.
            search(self, requested_names=names, cutoff_time_s=cutoff_time_s, budget_s=budget_s)
            if self.return_time_search.get("clock_budget_exhausted") is True:
                # Physical time has not caught up with the optional tool's wall
                # time. Do not start another candidate or dispatch from stale time.
                self.last_calculation_result = {
                    "status": "failed", "reason": "return_search_clock_debt"
                }
                self.stop_reason = "return_search_clock_debt"
                self.termination = "return_search_clock_debt_unresolved"
                self.event("m2_return_search_stopped", reason=self.stop_reason,
                           record_sha256=digest(self.return_time_search))
                return False
        if getattr(self, "return_time_search", None) is not None and self.return_time_search.get("search_cutoff_exceeded"):
            # An optional screen overrun does not cancel later opportunities.
            # Never start a forecast that lacks time for its bounded work and
            # the independently checked decision before departure.
            lead = (min(self.envelope["batch_timeout_s"], budget_s if budget_s is not None else float("inf"))
                    + reserved_decision_s(self.envelope)
                    + self.envelope["event_supervision"]["deadline_margin_s"])
            names = [name for name in names if self.times[name] > self.s.time_s + lead]
        if bounded:
            # The optional screen, including construction and cleanup, spends
            # the same deadline as the four independent admission trials.
            spent = max(self.s.time_s-start_sim, time.monotonic()-start_wall)
            budget_s = max(0.0, min(budget_s-spent, cutoff_time_s-self.s.time_s))
            if budget_s < self.p["integration"]["coast_dt_s"]:
                self.last_calculation_result = {
                    "status": "not_started", "reason": "forecast_batch_budget_exceeded"
                }
                return False
        return self.calculate_trials(
            names,
            budget_s=budget_s,
            cutoff_time_s=cutoff_time_s,
            synthetic_failure=synthetic_failure,
        )

    def calculate_trials(
        self, names, *, budget_s=None, cutoff_time_s=None, synthetic_failure=False
    ):
        bounded = budget_s is not None and bool(self.envelope.get("comparison_preparation"))
        self.last_calculation_result = {
            "status": "not_started",
            "reason": "forecast_budget_exhausted",
        }
        if not names or self.forecast_count + 4 * len(names) > self.envelope["maximum_forecasts"]:
            return False
        observed = self.observation()
        notice_sequence = notices(self.case, self.s.time_s)["sequence"]
        jobs = []
        for name in names:
            variants = [(o, 1.0) for o in uncertainty_origins(observed)] + [(observed, 0.5)]
            for origin, scale in variants:
                target = self.output / f"forecast-{self.forecast_count + len(jobs)}.json"
                job = (origin, self.p, self.times[name], self.scheduled, str(target), scale)
                bound_job = job + (True,) if getattr(self, "compact_artifacts", False) else job
                if self.envelope.get("resource_profile") == "unified_forecast_300_v1":
                    if self.envelope["forecast_timeout_s"] != 300. or not self.compact_artifacts:
                        raise ValueError("unified_forecast_resource_binding")
                    bound_job += (300.,)
                jobs.append(bound_job)
        self.forecast_count += len(jobs)
        pool = None
        operation_start, wall_start = self.s.time_s, time.monotonic()
        if bounded:
            cutoff_time_s = min(
                cutoff_time_s if cutoff_time_s is not None else float("inf"),
                operation_start + budget_s,
            )
        operation_index = len(self.pending)
        futures = []
        results = None
        failed = False

        def poll():
            if synthetic_failure:
                raise RuntimeError("approved_synthetic_forecast_cancellation")
            if all(f.done() for f in futures):
                return [f.result() for f in futures]
            return None

        try:
            pool = ProcessPoolExecutor(max_workers=self.envelope["maximum_workers"])
            futures = [pool.submit(prediction_job, job) for job in jobs]
            if bounded:
                remaining_budget = max(0.0, budget_s - (time.monotonic() - wall_start))
                results = self.wait(
                    "return_forecasts", poll, remaining_budget, cutoff_time_s=cutoff_time_s
                )
            else:
                results = self.wait("return_forecasts", poll, self.envelope["batch_timeout_s"])
        except Exception as error:
            if not bounded:
                raise
            failed = True
            self.last_calculation_result = {
                "status": "failed",
                "reason": "synthetic_tool_cancellation"
                if synthetic_failure
                else "forecast_worker_exception",
                "error_type": type(error).__name__,
            }
            if self.s.time_s == operation_start and (
                cutoff_time_s is None
                or self.s.time_s + self.p["integration"]["coast_dt_s"] <= cutoff_time_s + 1e-7
            ):
                self.step()
            if len(self.pending) == operation_index:
                self.pending.append(
                    {
                        "operation": "return_forecasts",
                        "start_time_s": operation_start,
                        "end_time_s": self.s.time_s,
                        "wall_time_s": time.monotonic() - wall_start,
                        "expired": True,
                        "cutoff_time_s": cutoff_time_s,
                        "cutoff_reached": False,
                        "error_type": type(error).__name__,
                    }
                )
            self.event("m1_tool_failed", **self.last_calculation_result)
        finally:
            # A timed-out numerical job cannot keep consuming resources silently.
            if pool is not None:
                if failed or any(not f.done() for f in futures):
                    for process in (getattr(pool, "_processes", None) or {}).values():
                        process.terminate()
                cleanup_started = time.monotonic()
                pool.shutdown(wait=True, cancel_futures=True)
                cleanup_wall = time.monotonic() - cleanup_started
            else:
                cleanup_wall = 0.0
            if bounded:
                # Count construction, exception handling and actual worker
                # shutdown, not just time spent polling prediction futures.
                total_wall = time.monotonic() - wall_start
                clock_overrun = False
                while self.s.time_s - operation_start < total_wall:
                    if cutoff_time_s is not None and (
                        self.s.time_s + self.p["integration"]["coast_dt_s"] > cutoff_time_s + 1e-7
                    ):
                        clock_overrun = True
                        break
                    self.step()
                    total_wall = time.monotonic() - wall_start
                operation = self.pending[operation_index]
                operation.update(
                    start_time_s=operation_start,
                    end_time_s=self.s.time_s,
                    wall_time_s=total_wall,
                    worker_cleanup_wall_time_s=cleanup_wall,
                    clock_accounting_overrun=clock_overrun,
                )
                if clock_overrun or total_wall > budget_s:
                    failed = True
                    operation["expired"] = True
                    self.last_calculation_result = {
                        "status": "failed",
                        "reason": "forecast_batch_budget_exceeded"
                        if results is None and operation.get("cutoff_reached")
                        else "forecast_clock_accounting_overrun"
                        if clock_overrun
                        else "forecast_total_wall_budget_exceeded",
                    }
                    self.event("m1_tool_failed", **self.last_calculation_result)
        if failed:
            return False
        if results is None or any(
            t["wall_time_s"] > self.envelope["forecast_timeout_s"] for t in results
        ):
            self.last_calculation_result = {
                "status": "incomplete",
                "reason": "forecast_batch_budget_exceeded"
                if bounded and self.pending[operation_index].get("cutoff_reached")
                else "forecast_expired_or_context_changed",
            }
            self.event("m1_tool_expired")
            return False
        if source_hashes() != self.sources:
            raise ValueError("m1_sources_changed")
        for index, name in enumerate(names):
            self.candidates[name] = {
                "id": name,
                "return_time_s": self.times[name],
                "scheduled_s": self.scheduled,
                "envelope_sha256": digest(self.envelope),
                "source_sha256": self.sources,
                "notice_sequence": notice_sequence,
                "signs": [0, -1, 1],
                "trials": results[index * 4 : index * 4 + 4],
                **(
                    {"deployment_binding": deepcopy(self.deployment_binding)}
                    if getattr(self, "deployment_binding", None) is not None
                    else {}
                ),
            }
        self.last_calculation_result = {"status": "completed", "reason": "predictions_available"}
        return True

    def select(self, name, basis):
        notice = notices(self.case, self.s.time_s)
        check = self.checks(notice).get(name)
        if not check or not check["accepted"]:
            return False
        # A valid reaffirmation is monitoring, not another plan change. Fresh
        # admission still runs, and new forecast evidence still spends a revision.
        if (
            self.selected == name
            and self.revisions
            and self.revisions[-1]["candidate_sha256"] == digest(self.candidates[name])
        ):
            return True
        if self.revision >= self.envelope["maximum_plan_revisions"]:
            return False
        self.revision += 1
        self.selected = name
        self.revisions.append(
            {
                "revision": self.revision,
                "time_s": self.s.time_s,
                "candidate_id": name,
                "candidate_sha256": digest(self.candidates[name]),
                "candidate_snapshot": deepcopy(self.candidates[name]),
                "basis": basis,
                "decision_request_id": self.decisions[-1]["request"]["request_id"],
                "notice": notice,
                "observation": self.observation(),
                "check": check,
            }
        )
        return True

    def ground_refresh_available(self, notice):
        # This channel only delivers the currently published coordinator notice.
        # Re-requesting the identical publication cannot add information. Fresh
        # vehicle telemetry still advances and every material event can wake us.
        return not any(row["notice"] == notice for row in self.ground_observations)

    def refresh_ground(self):
        # A requested separate status delivery consumes one finite control step.
        before = self.s.time_s
        previous = notices(self.case, before)
        self.step()
        row = notices(self.case, self.s.time_s)
        self.ground_observations.append(
            {
                "requested_s": before,
                "received_s": self.s.time_s,
                "notice": row,
                "information_changed": row != previous,
                "channel": "published_recovery_coordinator_status",
            }
        )
        self.event("m1_ground_status_received", sequence=row["sequence"])
        return row

    def wait_for_monitor(self, notice):
        # The current notice's explicit expiry is observable. The actor never
        # reads the synthetic scenario's unpublished future notice timestamp.
        due = self.s.time_s + self.envelope["monitor_interval_s"]
        if notice["expires_at_s"] > self.s.time_s:
            due = min(due, notice["expires_at_s"])
        due = min(due, self.times["next_orbit"] - 60.0)
        if due <= self.s.time_s or wait_reasons(self.envelope, due, self.scheduled):
            return False
        self.event("m1_bounded_wait", until_s=due)
        self.coast_to(due)
        return True

    def plan_return(self):
        last_sequence = notices(self.case, self.s.time_s)["sequence"]
        refreshed = set()
        stage = "return_options"
        while self.s.time_s < self.times["next_orbit"] - 40.0:
            notice = notices(self.case, self.s.time_s)
            if notice["sequence"] != last_sequence:
                stage = "updated_recovery_status"
                last_sequence = notice["sequence"]
            checks = self.checks(notice)
            ready = [n for n, c in checks.items() if c["accepted"]]
            future = [
                n
                for n, t in self.times.items()
                if t > self.s.time_s + self.envelope["batch_timeout_s"]
            ]
            need = [
                n
                for n in future
                if n not in self.candidates
                or self.candidates[n]["notice_sequence"] != notice["sequence"]
            ]
            can_compute = (
                need and self.forecast_count + 4 * len(need) <= self.envelope["maximum_forecasts"]
            )
            allowed = ["select_" + name for name in ready]
            if can_compute:
                allowed.append("evaluate_returns")
            if notice["sequence"] not in refreshed:
                allowed.append("refresh_observation")
            allowed += ["keep_plan" if self.selected else "wait_for_update"]
            action = self.choose(stage, allowed)
            if action is None:
                # Every fallback traverses exactly the same selection checks.
                if ready:
                    self.select(ready[0], "checked_fallback")
                    break
                if can_compute:
                    if not self.calculate(need):
                        break
                    stage = "updated_return_options"
                    continue
            elif action.startswith("select_"):
                basis = "ai" if self.envelope["mode"] == "live" else "fixture"
                if self.select(action.removeprefix("select_"), basis):
                    break
            elif action == "evaluate_returns":
                if not self.calculate(need):
                    break
                stage = "updated_return_options"
                continue
            elif action == "refresh_observation":
                self.refresh_ground()
                refreshed.add(notice["sequence"])
                stage = "after_ground_observation"
                continue
            # wait_for_update means actual bounded waiting, never immediate exit.
            self.event("m1_return_postponed", "bounded monitoring without a return command")
            if not self.wait_for_monitor(notice):
                break
            stage = "coast_monitoring"
        return self.selected is not None

    def run(self):
        if getattr(self, "monitor", None) is not None:
            return self.run_event_supervision()
        action = self.choose("normal_monitoring", ["evaluate_returns", "keep_plan"])
        # Keeping the nominal plan still requires its mandatory numerical check;
        # asking for alternatives actually expands the tool request.
        names = ["nominal"] if action == "keep_plan" else ["nominal", "next_orbit"]
        if not self.calculate(names):
            return self.finish()
        while self.plan_return():
            when = self.times[self.selected]
            withheld = False
            while self.s.time_s < when - 60.0:
                due = min(self.s.time_s + self.envelope["monitor_interval_s"], when - 60.0)
                self.coast_to(due)
                action = self.choose(
                    "selected_plan_monitoring",
                    ["keep_plan", "refresh_observation", "wait_for_update"],
                )
                if action == "refresh_observation":
                    self.refresh_ground()
                elif action == "wait_for_update":
                    self.event("m1_return_commitment_withheld", candidate_id=self.selected)
                    self.selected = None
                    self.refresh_ground()
                    withheld = True
                    break
            if withheld:
                continue
            self.coast_to(when)
            notice, observed = notices(self.case, self.s.time_s), self.observation()
            check = self.dispatch_check(self.selected, notice, observed)
            if check["accepted"] and source_hashes() == self.sources:
                self.dispatch = {
                    "candidate_id": self.selected,
                    "revision": self.revision,
                    "time_s": self.s.time_s,
                    "notice": notice,
                    "observation": observed,
                    "check": check,
                }
                self.controller.return_time_s = when
                self.event(
                    "m1_return_dispatch", "fresh independent area/resource/state checks passed"
                )
                while self.contact is None and self.s.time_s < self.scheduled + 10000.0:
                    self.step()
            else:
                self.event("m1_dispatch_rejected", reasons=check["reasons"])
            break
        return self.finish()

    @staticmethod
    def comparison_generation(notice):
        """Only already observed operational content, never future scenario data."""
        return digest(
            {key: value for key, value in notice.items() if key not in ("sequence", "issued_at_s")}
        )

    def comparison_booking(self):
        name = self.selected
        if name is None or name not in self.candidates:
            return None
        return {
            "candidate_id": name,
            "candidate_sha256": digest(self.candidates[name]),
            "return_time_s": self.times[name],
        }

    def prepare_comparison(self, notice):
        """Obtain bounded comparison evidence once per material notice generation.

        Preparation never selects a plan. Fresh existing predictions are reused;
        a failed optional batch cannot erase the prior admissible booking.
        """
        generation = self.comparison_generation(notice)
        if not hasattr(self, "comparison_generations"):
            self.comparison_generations, self.comparison_preparations = set(), []
        if generation == getattr(self, "comparison_last_generation", None):
            return
        initial = not self.comparison_generations
        self.comparison_generations.add(generation)
        self.comparison_last_generation = generation
        observed, start = self.observation(), self.s.time_s
        config = self.envelope["comparison_preparation"]
        configured = (
            config.get("initial_candidates", ["nominal"])
            if initial
            else config.get("update_candidates", ["nominal", "next_orbit"])
        )
        requested = [name for name in configured if self.times[name] > start]
        reused = [
            n
            for n in requested
            if n in self.candidates and not fresh_match(self.candidates[n], observed, self.envelope)
        ]
        missing = [n for n in requested if n not in reused]
        booking = self.comparison_booking()
        checked = self.checks(notice)
        protected = (
            booking["return_time_s"]
            if booking and checked.get(booking["candidate_id"], {}).get("accepted")
            else None
        )
        coast_dt = self.p["integration"]["coast_dt_s"]
        margin = self.envelope["event_supervision"]["deadline_margin_s"]
        admitted = [name for name, check in checked.items() if check["accepted"]]
        stop = min(
            [self.times[name] for name in admitted]
            + [self.times[n] for n in missing]
            + [self.scheduled + self.envelope["maximum_delay_s"]]
        )
        opportunity_cutoff = stop - reserved_decision_s(self.envelope) - margin - coast_dt
        available = max(0.0, min(self.envelope["batch_timeout_s"], opportunity_cutoff - start))
        cutoff = min(opportunity_cutoff, start + available)
        row = {
            "schema": "missionos.m1_comparison_preparation.v1",
            "preparation_id": f"{self.run_id}-comparison-{len(self.comparison_preparations)}",
            "generation_sha256": generation,
            "initial": initial,
            "observed_time_s": start,
            "observation": observed,
            "notice": deepcopy(notice),
            "selected_before": booking,
            "reused_candidate_ids": reused,
            "requested_candidate_ids": requested,
            "missing_candidate_ids": missing,
            "candidate_sha256_before": {n: digest(c) for n, c in self.candidates.items()},
            "forecast_count_before": self.forecast_count,
            "budget": {
                "maximum_forecasts": self.envelope["maximum_forecasts"],
                "batch_timeout_s": self.envelope["batch_timeout_s"],
                "decision_timeout_s": reserved_decision_s(self.envelope),
                "deadline_margin_s": margin,
                "coast_dt_s": coast_dt,
                "protected_burn_s": protected,
                "admissible_candidate_ids": admitted,
                "opportunity_cutoff_time_s": opportunity_cutoff,
                "cutoff_time_s": cutoff,
                "available_sim_s": available,
            },
            "start_time_s": start,
            "pending_operation_index": len(self.pending),
            "work_performed": False,
            "status": "ready",
            "reason": "fresh_predictions_reused",
        }
        if missing:
            minimum = config.get("minimum_start_budget_s", coast_dt)
            if available < minimum:
                row.update(status="not_started", reason="booking_or_candidate_deadline")
            elif self.forecast_count + 4 * len(missing) > self.envelope["maximum_forecasts"]:
                row.update(status="not_started", reason="forecast_budget_exhausted")
            else:
                synthetic = bool(
                    config.get("synthetic_tool_failure") == "cancel_on_material_update"
                    and not initial
                    and self.envelope["mode"] == "fixture"
                )
                row["synthetic_failure_injected"] = synthetic
                row["work_performed"] = True
                self.calculate(
                    missing, budget_s=available, cutoff_time_s=cutoff, synthetic_failure=synthetic
                )
                row.update(self.last_calculation_result)
        now_notice = notices(self.case, self.s.time_s)
        current_generation = self.comparison_generation(now_notice)
        remaining = [
            n
            for n in requested
            if n not in self.candidates
            or fresh_match(self.candidates[n], self.observation(), self.envelope)
        ]
        if current_generation != generation:
            row.update(status="interrupted", reason="material_notice_changed")
        row.update(
            end_time_s=self.s.time_s,
            pending_operation_end_index=len(self.pending),
            later_observation=self.observation(),
            selected_after=self.comparison_booking(),
            forecast_count_after=self.forecast_count,
            candidate_sha256_after={n: digest(c) for n, c in self.candidates.items()},
            candidate_snapshots_after={digest(c): deepcopy(c) for c in self.candidates.values()},
            remaining_candidate_ids=remaining,
        )
        self.comparison_preparations.append(row)
        self.comparison_context = {
            "preparation_id": row["preparation_id"],
            "generation_sha256": generation,
            "ready": not remaining and current_generation == generation,
            "reason": row["reason"],
            "missing_candidate_ids": remaining,
            "prepared_candidate_ids": [n for n in requested if n not in remaining],
        }
        self.event(
            "m1_comparison_prepared",
            preparation_id=row["preparation_id"],
            status=row["status"],
            reason=row["reason"],
        )

    def run_event_supervision(self):
        """One orbital loop; any observed event interrupts a coast or pending call.

        Bookings survive benign updates. Every action/fallback is reconsidered
        against current context; no superseded model result can dispatch.
        """
        stage = "normal_monitoring"
        initial = True
        margin = self.envelope["event_supervision"]["deadline_margin_s"]
        while self.s.time_s < self.times["next_orbit"] - 40.0 or (
            self.selected in self.times
            and self.s.time_s
            <= self.times[self.selected] + min(self.p["integration"]["coast_dt_s"], 2.001)
        ):
            if self.monitor.inhibited:
                self.event(
                    "m1_urgent_return_inhibited",
                    "observed health outside delegated domain; no model wait",
                )
                self.termination = "health_inhibited_unresolved"
                self.stop_reason = "observed_health_outside_delegated_domain"
                break
            notice = notices(self.case, self.s.time_s)
            comparison = bool(self.envelope.get("comparison_preparation"))
            if comparison:
                generation = self.comparison_generation(notice)
                self.prepare_comparison(notice)
                # Existing triggering events remain queued for choose() to bind;
                # only newly observed material changes require another batch.
                if (
                    self.monitor.inhibited
                    or self.comparison_generation(notices(self.case, self.s.time_s)) != generation
                ):
                    stage = "orbital_event"
                    continue
                initial = False
                notice = notices(self.case, self.s.time_s)
            checked = self.checks(notice)
            ready = [n for n, c in checked.items() if c["accepted"]]
            if self.selected not in ready:
                self.selected = None
            if (
                comparison
                and not ready
                and self.comparison_preparations[-1]["end_time_s"] == self.s.time_s
                and self.comparison_preparations[-1]["status"]
                in ("failed", "incomplete", "not_started")
            ):
                self.stop_reason = "comparison_preparation_unavailable"
                self.event(
                    "m1_comparison_stopped",
                    preparation_id=self.comparison_preparations[-1]["preparation_id"],
                    reason=self.stop_reason,
                )
                break
            future = [
                n
                for n, t in self.times.items()
                if t
                > self.s.time_s
                + self.envelope["batch_timeout_s"]
                + reserved_decision_s(self.envelope)
                + margin
            ]
            need = [
                n
                for n in future
                if n not in self.candidates
                or fresh_match(self.candidates[n], self.observation(), self.envelope)
            ]
            can_compute = bool(
                not comparison
                and need
                and self.forecast_count + 4 * len(need) <= self.envelope["maximum_forecasts"]
            )
            if initial:
                allowed = ["keep_plan", "evaluate_returns"]
            else:
                allowed = ["select_" + n for n in ready]
                if can_compute:
                    allowed.append("evaluate_returns")
                allowed += ["keep_plan"] if self.selected else ["wait_for_update"]
                if self.ground_refresh_available(notice):
                    allowed.append("refresh_observation")
            action = self.choose(stage, allowed)
            if self.monitor.pending or self.monitor.inhibited:
                stage = "orbital_event"
                continue
            if initial:
                initial = False
                names = ["nominal"] if action in (None, "keep_plan") else need
                calculated = self.calculate(names)
                if getattr(self, "stop_reason", None) == "return_search_clock_debt":
                    break
                if not calculated and not self.monitor.pending:
                    self.stop_reason = "forecast_unavailable"
                    break
                stage = "updated_return_options"
                continue
            # Re-read after the wait, including fallback. Decisions never reuse
            # pre-wait admission or availability.
            notice = notices(self.case, self.s.time_s)
            checked = self.checks(notice)
            ready = [n for n, c in checked.items() if c["accepted"]]
            if action is None:
                if self.selected in ready:
                    action = "keep_plan"
                elif ready:
                    if not self.select(ready[0], "checked_fallback"):
                        break
                    action = "keep_plan"
                elif can_compute:
                    action = "evaluate_returns"
                else:
                    action = "wait_for_update"
            if action == "evaluate_returns":
                calculated = self.calculate(need)
                if getattr(self, "stop_reason", None) == "return_search_clock_debt":
                    break
                if not calculated and not self.monitor.pending:
                    self.stop_reason = "forecast_unavailable"
                    break
                stage = "updated_return_options"
                continue
            if action.startswith("select_"):
                basis = "ai" if self.envelope["mode"] == "live" else "fixture"
                if not self.select(action.removeprefix("select_"), basis):
                    stage = "selection_rejected"
                    continue
            elif action == "refresh_observation":
                self.refresh_ground()
                stage = "after_ground_observation"
                continue
            elif action == "wait_for_update":
                self.selected = None
            if self.selected:
                when = self.times[self.selected]
                if self.s.time_s >= when - margin:
                    self.coast_to(when)
                    if self.monitor.pending:
                        stage = "orbital_event"
                        continue
                    notice = notices(self.case, self.s.time_s)
                    check = self.dispatch_check(self.selected, notice, self.observation())
                    if (
                        check["accepted"]
                        and not self.monitor.inhibited
                        and source_hashes() == self.sources
                    ):
                        self.dispatch = {
                            "candidate_id": self.selected,
                            "revision": self.revision,
                            "time_s": self.s.time_s,
                            "notice": notice,
                            "observation": self.observation(),
                            "check": check,
                        }
                        self.controller.return_time_s = when
                        self.event(
                            "m1_return_dispatch",
                            "event context and fresh independent checks passed",
                        )
                        while self.contact is None and self.s.time_s < self.scheduled + 10000.0:
                            self.step()
                    else:
                        self.event("m1_dispatch_rejected", reasons=check["reasons"])
                    break
                due = min(self.s.time_s + self.envelope["monitor_interval_s"], when - margin)
            else:
                due = min(
                    self.s.time_s + self.envelope["monitor_interval_s"],
                    self.times["next_orbit"] - margin,
                    notice["expires_at_s"],
                )
                if due <= self.s.time_s:
                    due = min(self.s.time_s + 60.0, self.times["next_orbit"] - margin)
            due = self.next_monitor_time(due)
            if wait_reasons(self.envelope, due, self.scheduled) or due <= self.s.time_s:
                break
            self.coast_to(due)
            stage = "orbital_event" if self.monitor.pending else "selected_plan_monitoring"
        return self.finish()

    def finish(self):
        if getattr(self, "monitor", None) and self.monitor.pending and not self.dispatch:
            reason = getattr(self, "stop_reason", None)
            if reason is None and self.s.time_s >= self.times["next_orbit"] - 40.0:
                reason = "return_decision_deadline_elapsed"
            if reason:
                self.event(
                    "m1_supervision_terminated",
                    event_ids=[e["id"] for e in self.monitor.pending],
                    termination=self.termination,
                    reason=reason,
                )
        self.samples.append(_sample(self.s, self.v, self.controller.phase))
        return {
            "schema": "missionos.starship_m1_study.v1",
            "run_id": self.run_id,
            "case": self.case,
            "envelope": self.envelope,
            "source_sha256": self.sources,
            "profile": self.p,
            "launch": self.launch,
            "execution_origin": self.origin,
            "scheduled_s": self.scheduled,
            "opportunities": self.times,
            "decisions": self.decisions,
            "pending_operations": self.pending,
            "plan_revisions": self.revisions,
            "candidates": self.candidates,
            "forecast_count": self.forecast_count,
            "dispatch": self.dispatch,
            "execution": {
                "samples": self.samples,
                "events": self.events,
                "final_state": asdict(self.s),
                "return_controller": self.controller.to_dict(),
                "outcome": {
                    "termination": self.termination,
                    "contact_receipt": self.contact,
                    **(
                        {"stop_reason": getattr(self, "stop_reason", None)}
                        if self.envelope.get("comparison_preparation") or getattr(self, "deployment_binding", None) is not None
                        else {}
                    ),
                },
            },
            "ground_observations": self.ground_observations,
            **(
                {
                    "supervision_events": self.monitor.events,
                    "supervision_observations": self.monitor.observations,
                }
                if getattr(self, "monitor", None) is not None
                else {}
            ),
            **(
                {"comparison_preparations": getattr(self, "comparison_preparations", [])}
                if self.envelope.get("comparison_preparation")
                else {}
            ),
            **(
                {
                    "deployment": deepcopy(self.deployment),
                    "deployment_binding": deepcopy(self.deployment_binding),
                }
                if getattr(self, "deployment", None) is not None
                else {}
            ),
            **(
                {"return_time_search": deepcopy(self.return_time_search)}
                if getattr(self, "return_time_search", None) is not None
                else {}
            ),
            "human_inflight_commands": 0,
            "physical_execution": False,
            "starship_vehicle_validated": False,
        }
