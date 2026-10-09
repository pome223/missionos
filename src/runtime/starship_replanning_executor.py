"""Credential-free M1 actor. AI decisions, predictions and execution are separate.

Launch/deployment use the existing production loop. Its exact final state is
continued with the shared ReturnController and finite plant stepper. Predictions
never replace execution state. The simulation clock advances during every tool
or model wait; between decisions the numerical simulation may run accelerated.
"""

from copy import deepcopy
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
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
from .starship_artifacts import write_verified_input

HANDOFF_TIME_S = 1000.7000000000407


def prediction_job(job):
    origin, profile, when, scheduled, destination, integration_scale = job
    profile = deepcopy(profile)
    for field in ("powered_dt_s", "coast_dt_s"):
        profile["integration"][field] *= integration_scale

    def expired(_signum, _frame):
        raise TimeoutError("m1_forecast_wall_deadline")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 180.0)
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
    result = write_verified_input(path, result)
    compact = {
        "origin": origin,
        "integration_scale": integration_scale,
        "final_state": result["final_state"],
        "outcome": result["outcome"],
        "return_time_s": when,
        "wall_time_s": result["wall_time_s"],
        "prediction_sha256": __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
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
    def __init__(self, profile, envelope, case, output, *, mailbox=None, run_id="standalone"):
        self.p, self.envelope, self.case = profile, envelope, case
        self.output, self.mailbox, self.run_id = (
            Path(output),
            Path(mailbox) if mailbox else None,
            run_id,
        )
        self.sources = source_hashes()
        self.launch = simulate(
            profile, return_policy="trimmed_state_terminal_v4", duration_s=HANDOFF_TIME_S
        )
        if (
            self.launch["outcome"]["payload_released_count"] != 26
            or self.launch["samples"][-1]["phase"] != "orbital_coast"
        ):
            raise ValueError("m1_post_deployment_handoff_unavailable")
        write_verified_input(self.output / "launch-checkpoint.json", self.launch)
        self.s = dyn.state_from_dict(self.launch["final_state"])
        self.origin = asdict(self.s)
        cutoff = next(
            e["time_s"] for e in self.launch["events"] if e["event"] == "orbit_cutoff_command"
        )
        self.scheduled = cutoff + profile["guidance"]["coast_before_return_s"]
        self.v = vehicle(profile, payload_count=0)
        observed = estimate(self.s)
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
            "trimmed_state_terminal_v4", 1e9, 26, allocation_enabled=True
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

    def wait(self, label, poll, limit):
        start_wall, start_sim = time.monotonic(), self.s.time_s
        value = None
        generation = self.monitor.generation if getattr(self, "monitor", None) else None
        while True:
            elapsed = time.monotonic() - start_wall
            while self.s.time_s - start_sim < elapsed:
                self.step()
            if elapsed >= limit:
                break
            if generation is not None and self.monitor.generation != generation:
                # A deadline is already being addressed by an in-flight forecast.
                # New facts still cancel it immediately and invalidate its context.
                changed = [e for e in self.monitor.pending if e["generation"] > generation]
                if label != "return_forecasts" or any(
                    e["kind"] != "candidate_deadline" for e in changed
                ):
                    break
            value = poll()
            if value is not None:
                break
            time.sleep(0.01)
        # A distinct measured state confirms even a very fast response.
        if self.s.time_s == start_sim:
            self.step()
        wall = time.monotonic() - start_wall
        while self.s.time_s - start_sim < wall:
            self.step()
        if (
            generation is not None
            and self.monitor.generation != generation
            and label == "return_forecasts"
        ):
            changed = [e for e in self.monitor.pending if e["generation"] > generation]
            if any(e["kind"] != "candidate_deadline" for e in changed):
                value = None
        self.pending.append(
            {
                "operation": label,
                "start_time_s": start_sim,
                "end_time_s": self.s.time_s,
                "wall_time_s": wall,
                "expired": value is None,
            }
        )
        return value

    def observation(self):
        return estimate(self.s)

    def checks(self, notice):
        observed = self.observation()
        return {
            name: admit(c, self.envelope, observed, notice, self.scheduled)
            for name, c in self.candidates.items()
        }

    def choose(self, stage, allowed):
        budget_exhausted = (
            sum(
                r["request_disposition"] in ("submitted", "synthetic_outage")
                for r in self.decisions
            )
            >= self.envelope["maximum_jev_calls"]
        )
        notice = notices(self.case, self.s.time_s)
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
            self.case == "event_timeout" and notice["sequence"] >= 2
        ):
            self.service_failed = True
        timeout_injected = self.service_failed
        if self.mailbox and not timeout_injected and not budget_exhausted and not deadline_fallback:
            publish(self.mailbox / "request.json", request)

        def poll():
            if timeout_injected:
                return None
            if not self.mailbox:
                from src.intelligence.starship_replanning import fixture_decision

                return fixture_decision(request, self.envelope)
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
            response = self.wait("model:" + stage, poll, self.envelope["decision_timeout_s"])
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
        if getattr(self, "monitor", None) is not None:
            record.update(later_generation=self.monitor.generation, interrupted_by_events=stale)
        self.decisions.append(record)
        self.event("m1_decision", stage, action=action, request_id=request["request_id"])
        return action

    def calculate(self, names):
        if self.forecast_count + 4 * len(names) > self.envelope["maximum_forecasts"]:
            return False
        observed = self.observation()
        notice_sequence = notices(self.case, self.s.time_s)["sequence"]
        jobs = []
        for name in names:
            variants = [(o, 1.0) for o in uncertainty_origins(observed)] + [(observed, 0.5)]
            for origin, scale in variants:
                target = self.output / f"forecast-{self.forecast_count + len(jobs)}.json"
                jobs.append((origin, self.p, self.times[name], self.scheduled, str(target), scale))
        self.forecast_count += len(jobs)
        pool = ProcessPoolExecutor(max_workers=self.envelope["maximum_workers"])
        futures = [pool.submit(prediction_job, job) for job in jobs]

        def poll():
            if all(f.done() for f in futures):
                return [f.result() for f in futures]
            return None

        try:
            results = self.wait("return_forecasts", poll, self.envelope["batch_timeout_s"])
        finally:
            # A timed-out numerical job cannot keep consuming resources silently.
            if any(not f.done() for f in futures):
                for process in pool._processes.values():
                    process.terminate()
            pool.shutdown(wait=True, cancel_futures=True)
        if results is None or any(
            t["wall_time_s"] > self.envelope["forecast_timeout_s"] for t in results
        ):
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
            }
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
            check = admit(
                self.candidates[self.selected], self.envelope, observed, notice, self.scheduled
            )
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
            checked = self.checks(notice)
            ready = [n for n, c in checked.items() if c["accepted"]]
            if self.selected not in ready:
                self.selected = None
            future = [
                n
                for n, t in self.times.items()
                if t
                > self.s.time_s
                + self.envelope["batch_timeout_s"]
                + self.envelope["decision_timeout_s"]
                + margin
            ]
            need = [
                n
                for n in future
                if n not in self.candidates
                or fresh_match(self.candidates[n], self.observation(), self.envelope)
            ]
            can_compute = bool(
                need and self.forecast_count + 4 * len(need) <= self.envelope["maximum_forecasts"]
            )
            if initial:
                allowed = ["keep_plan", "evaluate_returns"]
            else:
                allowed = ["select_" + n for n in ready]
                if can_compute:
                    allowed.append("evaluate_returns")
                allowed += ["keep_plan"] if self.selected else ["wait_for_update"]
                allowed.append("refresh_observation")
            action = self.choose(stage, allowed)
            if self.monitor.pending or self.monitor.inhibited:
                stage = "orbital_event"
                continue
            if initial:
                initial = False
                names = ["nominal"] if action in (None, "keep_plan") else need
                if not self.calculate(names) and not self.monitor.pending:
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
                if not self.calculate(need) and not self.monitor.pending:
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
                    check = self.checks(notice)[self.selected]
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
                "outcome": {"termination": self.termination, "contact_receipt": self.contact},
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
            "human_inflight_commands": 0,
            "physical_execution": False,
            "starship_vehicle_validated": False,
        }
