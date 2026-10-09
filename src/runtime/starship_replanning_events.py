"""Observed orbital events and their independent scalar audit.

No prediction, GNC, hidden fault schedule or model API is imported here. Inputs
are the currently delivered notice and the same observed channels available to
the mission director. An urgent coast-health violation latches return inhibition;
this module does not invent an alternative physical recovery trajectory.
"""

from copy import deepcopy
from hashlib import sha256
import json
import math


def _digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _inputs(observation, notice):
    state = observation["state"]
    now = state["time_s"]
    rate = state["omega_body_rad_s"]
    engines = [e["available"] for e in state["engine_states"]]
    if (
        not _number(now)
        or type(rate) not in (list, tuple)
        or len(rate) != 3
        or any(not _number(x) for x in rate)
        or not engines
        or any(type(x) is not bool for x in engines)
        or type(notice.get("sequence")) is not int
        or notice["sequence"] < 0
        or not _number(notice.get("issued_at_s"))
        or not _number(notice.get("expires_at_s"))
        or not notice["issued_at_s"] <= now
        or not notice["issued_at_s"] <= notice["expires_at_s"]
    ):
        raise ValueError("invalid_or_unpublished_supervision_input")
    # Reject non-finite numbers anywhere in the delivered notice before hashing.
    notice_hash = _digest(notice)
    return now, list(rate), engines, notice_hash


class EventMonitor:
    """Latch observable changes at every finite orbital plant step.

    Repeated polls are harmless. Multiple simultaneous changes share a context
    generation, and remain pending until the next decision request drains them.
    """

    def __init__(self, envelope, times):
        self.maximum_rate = envelope["event_supervision"]["maximum_coast_body_rate_rad_s"]
        margin = envelope["event_supervision"].get("deadline_margin_s")
        if not _number(margin) or margin < 0:
            raise ValueError("invalid_supervision_deadline_margin")
        self.lead_s = envelope["batch_timeout_s"] + envelope["decision_timeout_s"] + margin
        if (
            not _number(self.maximum_rate)
            or self.maximum_rate <= 0
            or not _number(self.lead_s)
            or self.lead_s <= 0
            or not times
            or any(type(k) is not str or not _number(v) for k, v in times.items())
        ):
            raise ValueError("invalid_supervision_contract")
        self.times = dict(times)
        self.generation = 0
        self.pending = []
        self.events = []
        self.observations = []
        self.inhibited = False
        self._notice_hash = None
        self._health = None
        self._expired = set()
        self._deadlines = set()

    def poll(self, observation, notice, selected):
        now, rate, engines, notice_hash = _inputs(observation, notice)
        if self.observations and now < self.observations[-1]["time_s"]:
            raise ValueError("supervision_clock_reversed")
        if selected is not None and selected not in self.times:
            raise ValueError("unknown_supervision_selection")
        above_rate = sum(x * x for x in rate) > self.maximum_rate * self.maximum_rate
        health = (tuple(engines), above_rate)
        urgent = not all(engines) or above_rate
        changed_notice = notice_hash != self._notice_hash
        row = {
            "index": len(self.observations),
            "time_s": now,
            "notice_sha256": notice_hash,
            "notice": deepcopy(notice) if changed_notice else None,
            "engine_available": engines,
            "body_rate_rad_s": rate,
            "selected": selected,
        }
        self.observations.append(row)
        changes = []
        if self._notice_hash is not None and changed_notice:
            changes.append(("notice_updated", False, {}))
        if now >= notice["expires_at_s"] and notice_hash not in self._expired:
            changes.append(("notice_expired", False, {}))
            self._expired.add(notice_hash)
        if (self._health is None and urgent) or (
            self._health is not None and health != self._health
        ):
            changes.append(("health_changed", urgent, {}))
        if urgent:
            self.inhibited = True
        for name, when in sorted(self.times.items(), key=lambda item: (item[1], item[0])):
            due = when - self.lead_s
            if now >= due and name not in self._deadlines:
                changes.append(("candidate_deadline", False, {"candidate_id": name, "due_s": due}))
                self._deadlines.add(name)
        self._notice_hash, self._health = notice_hash, health
        added = []
        if changes:
            self.generation += 1
            for kind, is_urgent, extra in changes:
                event = {
                    "id": f"event-{len(self.events) + 1}",
                    "kind": kind,
                    "detected_at_s": now,
                    "generation": self.generation,
                    "urgent": is_urgent,
                    "observation_index": row["index"],
                    "notice_sha256": notice_hash,
                    **extra,
                }
                self.events.append(event)
                self.pending.append(event)
                added.append(event)
        return deepcopy(added)

    def drain(self):
        result = deepcopy(self.pending)
        self.pending.clear()
        return result


def verify_events(study, envelope, *, expected_case=None):
    """Replay published-input transitions without calling EventMonitor.

    Production verification also supplies expected_case, binding every delivered
    notice to the registered synthetic scenario at the observed time. This does
    not establish fidelity of real sensors or an external notification service.
    """
    issues = []
    try:
        observations = study["supervision_observations"]
        recorded_events = study["supervision_events"]
        times = study["opportunities"]
        maximum_rate = envelope["event_supervision"]["maximum_coast_body_rate_rad_s"]
        margin = envelope["event_supervision"].get("deadline_margin_s")
        if not _number(margin) or margin < 0:
            return ["supervision_deadline_margin_invalid"]
        lead = envelope["batch_timeout_s"] + envelope["decision_timeout_s"] + margin
        registered_notices = None
        if expected_case is not None:
            from .starship_replanning import SCENARIOS, notices

            if expected_case not in SCENARIOS.values():
                return ["supervision_unknown_registered_case"]
            registered_notices = notices
        maximum_gap = study["profile"]["integration"]["coast_dt_s"] + 1e-6
        if (
            not observations
            or not _number(maximum_rate)
            or maximum_rate <= 0
            or not _number(maximum_gap)
            or maximum_gap <= 0
            or not times
            or any(not _number(when) for when in times.values())
        ):
            return ["supervision_inputs_missing_or_invalid"]
        previous_time = study["execution_origin"]["time_s"]
        previous_hash = None
        previous_health = None
        current_notice = None
        expired = set()
        deadlines = set()
        generation = 0
        expected_events = []
        health_inhibited_at = None
        for index, row in enumerate(observations):
            now = row["time_s"]
            rate, engines = row["body_rate_rad_s"], row["engine_available"]
            if (
                row["index"] != index
                or not _number(now)
                or now < previous_time
                or now - previous_time > maximum_gap
                or type(engines) is not list
                or not engines
                or any(type(x) is not bool for x in engines)
                or type(rate) is not list
                or len(rate) != 3
                or any(not _number(x) for x in rate)
                or (row["selected"] is not None and row["selected"] not in times)
            ):
                issues.append("supervision_observation_coverage")
            previous_time = now
            delivered = row["notice"]
            if delivered is not None:
                if previous_hash is not None and _digest(delivered) == previous_hash:
                    issues.append("supervision_redundant_notice_payload")
                current_notice = delivered
            if (
                current_notice is None
                or _digest(current_notice) != row["notice_sha256"]
                or type(current_notice.get("sequence")) is not int
                or current_notice["sequence"] < 0
                or not _number(current_notice.get("issued_at_s"))
                or not _number(current_notice.get("expires_at_s"))
                or not current_notice["issued_at_s"] <= now
                or current_notice["expires_at_s"] < current_notice["issued_at_s"]
            ):
                issues.append("supervision_notice_evidence")
                return sorted(set(issues))
            notice_hash = row["notice_sha256"]
            if registered_notices is not None and notice_hash != _digest(
                registered_notices(expected_case, now)
            ):
                issues.append("supervision_registered_notice_mismatch")
            too_fast = sum(x * x for x in rate) > maximum_rate * maximum_rate
            health = (tuple(engines), too_fast)
            invalid_health = not all(engines) or too_fast
            changes = []
            if previous_hash is not None and notice_hash != previous_hash:
                changes.append(("notice_updated", False, {}))
            if now >= current_notice["expires_at_s"] and notice_hash not in expired:
                changes.append(("notice_expired", False, {}))
                expired.add(notice_hash)
            if (previous_health is None and invalid_health) or (
                previous_health is not None and health != previous_health
            ):
                changes.append(("health_changed", invalid_health, {}))
            if invalid_health and health_inhibited_at is None:
                health_inhibited_at = now
            for name in sorted(times, key=lambda key: (times[key], key)):
                due = times[name] - lead
                if now >= due and name not in deadlines:
                    changes.append(
                        ("candidate_deadline", False, {"candidate_id": name, "due_s": due})
                    )
                    deadlines.add(name)
            if changes:
                generation += 1
                for kind, urgent, extra in changes:
                    expected_events.append(
                        {
                            "id": f"event-{len(expected_events) + 1}",
                            "kind": kind,
                            "detected_at_s": now,
                            "generation": generation,
                            "urgent": urgent,
                            "observation_index": index,
                            "notice_sha256": notice_hash,
                            **extra,
                        }
                    )
            previous_hash, previous_health = notice_hash, health
        if recorded_events != expected_events:
            issues.append("supervision_event_replay")
        dispatch = study["dispatch"]
        end_time = dispatch["time_s"] if dispatch else study["execution"]["final_state"]["time_s"]
        if not 0 <= end_time - observations[-1]["time_s"] <= maximum_gap:
            issues.append("supervision_end_coverage")

        def at_time(now):
            return max(
                (e["generation"] for e in expected_events if e["detected_at_s"] <= now),
                default=0,
            )

        drained = set()
        previous_request_time = -math.inf
        for record in study["decisions"]:
            request = record["request"]
            request_time, later_time = request["time_s"], record["later_time_s"]
            current = at_time(request_time)
            later = at_time(later_time)
            available = [
                e["id"]
                for e in expected_events
                if e["detected_at_s"] <= request_time and e["id"] not in drained
            ]
            if (
                not _number(request_time)
                or not _number(later_time)
                or request_time < previous_request_time
                or not observations[0]["time_s"]
                <= request_time
                <= later_time
                <= observations[-1]["time_s"]
                or (
                    request_time == later_time
                    and record.get("request_disposition") != "deadline_fallback"
                )
                or type(request["context_generation"]) is not int
                or request["context_generation"] != current
                or type(record["later_generation"]) is not int
                or record["later_generation"] != later
            ):
                issues.append("supervision_decision_context")
            if request["trigger_event_ids"] != available:
                issues.append("supervision_event_drain")
            latest_input = next(
                row for row in reversed(observations) if row["time_s"] <= request_time
            )
            if _digest(request["notice"]) != latest_input["notice_sha256"]:
                issues.append("supervision_request_notice_binding")
            for event in expected_events:
                if event["id"] not in available:
                    continue
                latency_limit = envelope["decision_timeout_s"] + maximum_gap
                if event["kind"] == "candidate_deadline":
                    latency_limit += envelope["batch_timeout_s"]
                if request_time - event["detected_at_s"] > latency_limit:
                    issues.append("supervision_event_response_late")
            drained.update(available)
            interrupted = later > current
            if (
                type(record["interrupted_by_events"]) is not bool
                or record["interrupted_by_events"] != interrupted
                or (interrupted and record["accepted_action"] is not None)
            ):
                issues.append("supervision_stale_response_accepted")
            previous_request_time = request_time
        undrained = [e["id"] for e in expected_events if e["id"] not in drained]
        terminal_dispositions = [
            e
            for e in study["execution"].get("events", [])
            if e.get("event") == "m1_supervision_terminated"
        ]
        if dispatch:
            if health_inhibited_at is not None and health_inhibited_at <= dispatch["time_s"]:
                issues.append("supervision_dispatch_after_health_inhibit")
            if undrained:
                issues.append("supervision_dispatch_with_pending_event")
            if terminal_dispositions:
                issues.append("supervision_termination_disposition_invalid")
        elif undrained:
            # A no-dispatch flag alone cannot silently dispose of pending input.
            # Require an explicit terminal disposition tied to the actual outcome.
            outcome = study["execution"].get("outcome", {})
            terminal = terminal_dispositions[0] if len(terminal_dispositions) == 1 else {}
            termination = outcome.get("termination")
            reason = terminal.get("reason")
            valid = (
                type(termination) is str
                and termination.endswith("_unresolved")
                and outcome.get("contact_receipt") is None
                and terminal.get("time_s") == end_time
                and terminal.get("event_ids") == undrained
                and terminal.get("termination") == termination
                and type(reason) is str
                and bool(reason.strip())
            )
            if health_inhibited_at is not None:
                urgent_evidence = [
                    e
                    for e in study["execution"].get("events", [])
                    if e.get("event") == "m1_urgent_return_inhibited"
                    and e.get("time_s") == end_time
                ]
                valid = (
                    valid
                    and termination == "health_inhibited_unresolved"
                    and reason == "observed_health_outside_delegated_domain"
                    and len(urgent_evidence) == 1
                    and end_time - health_inhibited_at <= maximum_gap
                )
            elif reason == "return_decision_deadline_elapsed":
                valid = (
                    valid
                    and termination == "return_unresolved"
                    and end_time >= max(times.values()) - 40.0
                )
            elif reason == "forecast_unavailable":
                failed_tools = [
                    event
                    for event in study["execution"].get("events", [])
                    if event.get("event") == "m1_tool_expired" and event.get("time_s") == end_time
                ]
                failed_operations = [
                    operation
                    for operation in study.get("pending_operations", [])
                    if operation.get("operation") == "return_forecasts"
                    and operation.get("expired") is True
                    and operation.get("end_time_s") == end_time
                    and _number(operation.get("start_time_s"))
                    and operation["start_time_s"] <= end_time
                ]
                valid = (
                    valid
                    and termination == "return_unresolved"
                    and len(failed_tools) == 1
                    and len(failed_operations) == 1
                )
            else:
                valid = False
            if not valid:
                issues.append("supervision_event_unhandled")
        elif terminal_dispositions:
            issues.append("supervision_termination_disposition_invalid")
        return sorted(set(issues))
    except (KeyError, TypeError, ValueError, OverflowError, IndexError):
        return ["invalid_supervision_record"]
