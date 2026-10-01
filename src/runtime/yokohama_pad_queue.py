"""CPU-only occupied-pad scene and aircraft-to-MissionOS decision mailbox.

The lead aircraft is a scripted Gazebo entity, not a second PX4 aircraft.
The host judge is a deterministic fixture, not VLA/WAM or an LLM. An optional
mission judge (a Gateway-hosted LLM or a fixture) is asked only when that
judge would allow entry, and can only add a bounded wait. Simulator poses
stand in for a future perception source. Proposals grant no authority.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import threading
import time
import xml.etree.ElementTree as ET

try:
    from src.runtime.yokohama_payload import atomic_json, digest, fresh_pose
    from src.runtime.yokohama_pad_advisory_contract import selected_action, summarize
except ModuleNotFoundError:  # frozen worker copy inside the isolated container
    from yokohama_payload import atomic_json, digest, fresh_pose
    from yokohama_pad_advisory_contract import selected_action, summarize

WAIT, ENTER = "wait_at_current_hold", "enter_delivery_approach"
# Mission judge statuses that hold the aircraft, and those that leave the Rules action.
JUDGE_WAITS = {"pending", "hold"}
JUDGE_PASSES = {"no_objection", "unavailable", "invalid", "budget_exhausted", "decisions_exhausted"}


def segment_distance(point, start, end):
    d = [b - a for a, b in zip(start, end)]
    n = sum(v * v for v in d)
    f = max(0, min(1, sum((p - a) * v for p, a, v in zip(point, start, d)) / n)) if n else 0
    return math.dist(point, [a + f * v for a, v in zip(start, d)])


def extend_world(root, bundle, world):
    from shapely.geometry import LineString, shape
    from src.runtime.yokohama_scene import camera, sha256, to_source

    pad = world["payload_delivery"]["pad_world_xyz_m"]
    wait = world["points"][2]["world_xyz_m"]
    approach = world["points"][3]["world_xyz_m"]
    start = [pad[0] + 0.8, pad[1] + 0.8, pad[2] + 1.2]
    up = [*start[:2], pad[2] + 7]
    footprints = json.loads((bundle / "collision-footprints.geojson").read_text())["features"]
    choices = []
    for k in range(16):
        end = [
            up[0] + 12 * math.cos(k * math.pi / 8),
            up[1] + 12 * math.sin(k * math.pi / 8),
            up[2],
        ]
        line = LineString(to_source([start, up, end], world["frame"])[:, :2])
        clearance = min(line.distance(shape(f["geometry"])) for f in footprints)
        route_clearance = min(
            segment_distance(end, wait, approach),
            segment_distance(end, approach, world["payload_delivery"]["hover_world_xyz_m"]),
        )
        if clearance > 3 and route_clearance > 6:
            choices.append((clearance, end))
    if not choices:
        raise ValueError("No mapped departure corridor for the scripted lead aircraft")
    clearance, end = max(choices)
    path = root / "models/worlds/default.sdf"
    tree = ET.parse(path)
    node = tree.getroot().find("world")

    def model(name, xyz):
        m = ET.SubElement(node, "model", name=name)
        ET.SubElement(m, "static").text = "true"
        ET.SubElement(m, "pose").text = " ".join(map(str, [*xyz, 0, 0, 0]))
        return ET.SubElement(m, "link", name="link")

    def box(link, name, xyz, size, color):
        for kind in ("visual", "collision"):
            part = ET.SubElement(link, kind, name=name)
            ET.SubElement(part, "pose").text = " ".join(map(str, [*xyz, 0, 0, 0]))
            ET.SubElement(ET.SubElement(ET.SubElement(part, "geometry"), "box"), "size").text = size
            if kind == "visual":
                mat = ET.SubElement(part, "material")
                ET.SubElement(mat, "diffuse").text = color
                ET.SubElement(mat, "ambient").text = color

    link = model("queue_lead", start)
    box(link, "body", [0, 0, 0], ".8 .6 .25", ".95 .5 .05 1")
    for i, (x, y) in enumerate([(-0.6, -0.6), (-0.6, 0.6), (0.6, -0.6), (0.6, 0.6)]):
        box(link, f"rotor_{i}", [x, y, 0.1], ".5 .5 .04", ".08 .1 .12 1")
    box(
        model("queue_parcel", [*start[:2], start[2] - 0.5]),
        "cargo",
        [0, 0, 0],
        ".3 .3 .3",
        ".1 .7 .4 1",
    )
    camera_link = model("queue_camera", [0, 0, 0])
    camera(
        camera_link,
        "queue_rgb",
        [pad[0] + 16, pad[1] - 16, pad[2] + 12],
        [0, math.atan2(10, math.sqrt(512)), 3 * math.pi / 4],
        "/yokohama/queue",
        rate_hz=4,
    )
    tree.write(path, encoding="utf-8", xml_declaration=True)
    world["world_sha256"] = sha256(path)
    world["pad_queue"] = dict(
        schema="missionos.yokohama-pad-queue.v1",
        lead_entity="queue_lead",
        parcel_entity="queue_parcel",
        lead_start_xyz_m=start,
        lead_up_xyz_m=up,
        lead_end_xyz_m=end,
        departure_building_clearance_m=clearance,
        wait_xyz_m=wait,
        approach_xyz_m=approach,
        pad_xyz_m=pad,
        trigger_phase="02-D3",
        occupied_duration_sim_s=15,
        unload_duration_sim_s=5,
        ascent_duration_sim_s=6,
        departure_duration_sim_s=12,
        stable_clear_sim_s=5,
        maximum_wait_sim_s=90,
        maximum_wait_wall_s=180,
        maximum_sample_gap_sim_s=2,
        maximum_observation_age_s=1,
        response_max_age_s=2,
        minimum_battery_fraction=0.2,
        pad_exclusion_radius_m=6,
        approach_exclusion_radius_m=3,
        lead_motion="scripted poses; no lead autopilot or unloading physics",
        observation_source="Gazebo pose telemetry, not image perception",
        judge_backend="deterministic CPU fixture; no learned model",
        approved_actions=["wait_at_current_hold", "enter_delivery_approach"],
        communication_fallback="retain current AP hold within local limits; terminate owned SITL on deadline",
    )
    return world


def clearance(config, row):
    p = config["world"]["pad_queue"]
    if (
        row.get("run_id") != config["run_id"]
        or row.get("world_sha256") != config["world"]["world_sha256"]
    ):
        raise ValueError("Foreign pad observation")
    if not math.isfinite(row["sim_s"]) or not math.isfinite(row["wall_s"]):
        raise ValueError("Invalid pad clock")
    if not all(
        fresh_pose(row, name, p["maximum_observation_age_s"]) for name in ("vehicle", "queue_lead")
    ):
        raise ValueError("Stale or missing pad pose")
    lead = row["queue_lead"]["xyz"]
    horizontal = math.dist(lead[:2], p["pad_xyz_m"][:2])
    route = min(
        segment_distance(lead, p["wait_xyz_m"], p["approach_xyz_m"]),
        segment_distance(
            lead, p["approach_xyz_m"], config["world"]["payload_delivery"]["hover_world_xyz_m"]
        ),
    )
    return dict(
        pad_clear=horizontal > p["pad_exclusion_radius_m"],
        approach_clear=route > p["approach_exclusion_radius_m"],
        horizontal_distance_m=horizontal,
        approach_distance_m=route,
    )


def approved_hold(config, hold=None):
    """The authored wait point, or one bounded model endpoint beyond it."""
    p = config["world"]["pad_queue"]
    if hold is None:
        return p["wait_xyz_m"]
    limit = p.get("model_hold_max_offset_m")
    if (
        limit is None
        or len(hold) != 3
        or not all(math.isfinite(v) for v in hold)
        or math.dist(hold, p["wait_xyz_m"]) > limit
    ):
        raise ValueError("Pad hold outside the approved model-approach bound")
    return hold


def require_wait(config, row, hold=None):
    p = config["world"]["pad_queue"]
    hold = approved_hold(config, hold)
    clearance(config, row)
    speed = row.get("velocity_ned", [])
    battery = row.get("battery_fraction")
    if not (
        row["phase"] in (p["trigger_phase"], "03-DELIVERY")
        and row["nav_state"] == 4
        and row["arming_state"] == 2
        and row["landed"] is False
        and row.get("position_valid") is True
        and len(speed) == 3
        and all(math.isfinite(v) for v in speed)
        and math.hypot(*speed) <= 0.3
        and isinstance(battery, (int, float))
        and math.isfinite(battery)
        and battery >= p["minimum_battery_fraction"]
        and math.dist(row["vehicle"]["xyz"], hold) <= 0.6
    ):
        raise ValueError("Aircraft cannot wait within the approved pad hold")


def clear_window(config, rows, hold=None):
    p = config["world"]["pad_queue"]
    if len(rows) < 2:
        return False
    for row in rows:
        require_wait(config, row, hold)
        c = clearance(config, row)
        if not (c["pad_clear"] and c["approach_clear"]):
            return False
    return (
        rows[-1]["sim_s"] - rows[0]["sim_s"] >= p["stable_clear_sim_s"]
        and all(
            0 < b["sim_s"] - a["sim_s"] <= p["maximum_sample_gap_sim_s"]
            for a, b in zip(rows, rows[1:])
        )
        and all(
            a["reset_counters"] == rows[0]["reset_counters"]
            and a["queue_lead"]["id"] == rows[0]["queue_lead"]["id"]
            and a["vehicle"]["id"] == rows[0]["vehicle"]["id"]
            for a in rows
        )
    )


def make_request(config, sequence, rows, camera_history=None, hold=None):
    require_wait(config, rows[-1], hold)
    r = dict(
        schema="missionos.aircraft-situation-request.v1",
        run_id=config["run_id"],
        config_sha256=digest(config),
        sequence=sequence,
        incident="delivery_pad_occupied",
        observations=rows,
        source="simulator_pose_fixture",
    )
    if config["world"].get("pad_state_advisory"):
        r["pad_camera_history"] = camera_history
    if hold is not None:
        r["hold_xyz_m"] = hold
    return dict(r, request_id=digest(r))


def propose(config, request):
    content = dict(request)
    request_id = content.pop("request_id")
    if (
        digest(content) != request_id
        or request["config_sha256"] != digest(config)
        or request["run_id"] != config["run_id"]
    ):
        raise ValueError("Unbound aircraft request")
    rows = request["observations"]
    hold = request.get("hold_xyz_m")
    require_wait(config, rows[-1], hold)
    action = (
        "enter_delivery_approach" if clear_window(config, rows, hold) else "wait_at_current_hold"
    )
    return dict(
        schema="missionos.aircraft-response-proposal.v1",
        request_id=request_id,
        run_id=config["run_id"],
        sequence=request["sequence"],
        config_sha256=digest(config),
        proposed_action=action,
        observation_sha256=digest(rows[-1]),
        backend="fixture",
        approval_granted=False,
        dispatch_authority_created=False,
    )


def require_response(config, request, response, current):
    """Executor-side Rules revalidate an untrusted proposal against fresh facts."""
    require_wait(config, current, request.get("hold_xyz_m"))
    expected = propose(config, request)
    if config["world"].get("pad_state_advisory"):
        receipt = response.get("advisory", {})
        judgment_hash = response.get("mission_assurance_sha256", "")
        if (
            not isinstance(judgment_hash, str)
            or len(judgment_hash) != 64
            or any(c not in "0123456789abcdef" for c in judgment_hash)
        ):
            raise ValueError("Missing mission judgment identity")
        expected.update(
            proposed_action=selected_action(config, request, receipt, expected["proposed_action"]),
            backend="cpu_state_advisory_fixture",
            advisory=receipt,
            mission_assurance_sha256=judgment_hash,
        )
    judge = config["world"]["pad_queue"].get("mission_judge")
    if judge:
        expected.update(
            judge_overlay(judge, expected["proposed_action"], response.get("mission_judge"))
        )
    if response != expected:
        raise ValueError("Foreign, changed or unexpected mission response")
    p = config["world"]["pad_queue"]
    previous = request["observations"][-1]
    if not all(
        0 <= current[k] - previous[k] <= p["response_max_age_s"] for k in ("sim_s", "wall_s")
    ):
        raise ValueError("Expired mission response")
    if (
        current["reset_counters"] != previous["reset_counters"]
        or current["vehicle"]["id"] != previous["vehicle"]["id"]
        or current["queue_lead"]["id"] != previous["queue_lead"]["id"]
    ):
        raise ValueError("Observation identity or estimator reset changed")
    action = response["proposed_action"]
    if judge and "wait_deadline_wall_s" in response["mission_judge"]:
        receipt = response["mission_judge"]
        deadline = receipt["wait_deadline_wall_s"]
        # A stale host WAIT cannot extend the approved judge budget on aircraft.
        if receipt["prior_action"] == ENTER and (
            (receipt["status"] in JUDGE_WAITS and current["wall_s"] >= deadline)
            or (
                action == ENTER
                and current["wall_s"] > deadline
                and receipt["status"] != "decisions_exhausted"
            )
        ):
            raise ValueError("Mission judge release exceeded wall-clock deadline")
    if action not in p["approved_actions"] or not config.get("operator_approval"):
        raise ValueError("Action outside preapproved simulator scope")
    if action == "enter_delivery_approach":
        c = clearance(config, current)
        if not c["pad_clear"] or not c["approach_clear"]:
            raise ValueError("Pad reoccupied; old continuation rejected")
    return action


def judge_overlay(policy, prior, receipt):
    """Executor-side check: a mission judge may only turn a Rules entry into a wait."""
    if not isinstance(receipt, dict) or receipt.get("prior_action") != prior:
        raise ValueError("Mission judge receipt not bound to the Rules action")
    status = receipt.get("status")
    if prior == WAIT:
        if status != "not_consulted":
            raise ValueError("Mission judge consulted without a Rules entry")
        action = WAIT
    elif status in JUDGE_WAITS:
        added = receipt.get("added_wait_s")
        if (
            isinstance(added, bool)
            or not isinstance(added, (int, float))
            or not 0 <= added <= policy["max_added_wait_s"]
        ):
            raise ValueError("Mission judge wait outside its budget")
        action = WAIT
    elif status in JUDGE_PASSES:
        action = prior
    else:
        raise ValueError("Unknown mission judge status")
    return dict(proposed_action=action, mission_judge=receipt)


def judge_situation(config, request, response):
    """Facts for the mission judge, with derived margins; thresholds stay with the Rules.

    The judge misread raw distances against the radius, so margins, the radial
    speed (positive when moving away) and the time to the radius are computed here.
    """
    p = config["world"]["pad_queue"]
    rows = request["observations"]
    first, last = clearance(config, rows[0]), clearance(config, rows[-1])
    window = rows[-1]["sim_s"] - rows[0]["sim_s"]
    speed = (
        (last["horizontal_distance_m"] - first["horizontal_distance_m"]) / window
        if window > 0
        else 0.0
    )
    margin = last["horizontal_distance_m"] - p["pad_exclusion_radius_m"]
    approaching = speed < -0.2
    situation = dict(
        rules_action=response["proposed_action"],
        clear_window_s=round(window, 1),
        required_clear_window_s=p["stable_clear_sim_s"],
        lead_horizontal_distance_to_pad_m=round(last["horizontal_distance_m"], 1),
        pad_exclusion_radius_m=p["pad_exclusion_radius_m"],
        lead_margin_outside_exclusion_m=round(margin, 1),
        lead_radial_speed_mps=round(speed, 2),
        lead_motion="approaching" if approaching else "moving_away" if speed > 0.2 else "holding",
        seconds_to_exclusion_at_current_speed=round(margin / -speed, 1) if approaching else None,
        lead_approach_corridor_margin_m=round(
            last["approach_distance_m"] - p["approach_exclusion_radius_m"], 1
        ),
        lead_altitude_m=round(rows[-1]["queue_lead"]["xyz"][2], 1),
        battery_fraction=rows[-1].get("battery_fraction"),
    )
    advisory = response.get("advisory")
    if advisory:
        situation["camera_advisory"] = dict(
            signal=summarize(config, request, advisory),
            status=advisory.get("status"),
            supported=advisory.get("forecast", {}).get("supported"),
        )
    return situation


def fixture_judgment(judge_request):
    return dict(
        judge_request_id=judge_request["judge_request_id"],
        judge_status="valid",
        decision=dict(
            observation_id=judge_request["observation_id"],
            action="enter",
            wait_seconds=0,
            rationale="固定の判定です（LLMなし）。ルールの進入判断に異論はありません。",
        ),
        invocation=dict(invocation_kind="deterministic_fixture"),
    )


def validate_judgment(judge_request, answer):
    """The host accepts only a bound, bounded enter/wait decision; anything else is invalid."""
    decision = answer.get("decision") if isinstance(answer, dict) else None
    if (
        not isinstance(decision, dict)
        or answer.get("judge_request_id") != judge_request["judge_request_id"]
        or answer.get("judge_status") != "valid"
        or decision.get("observation_id") != judge_request["observation_id"]
        or decision.get("action") not in ("enter", "wait")
        or not isinstance(decision.get("rationale"), str)
        or not decision["rationale"].strip()
    ):
        return None
    seconds = decision.get("wait_seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    if decision["action"] == "wait":
        if not 1 <= seconds <= judge_request["remaining_wait_seconds"]:
            return None
    elif seconds != 0:
        return None
    return decision


class MissionJudgeGate:
    """Host side: consult a mission judge (Gateway LLM or fixture) before a Rules entry.

    The judge is asked only when the Rules/advisory action is entry. While its
    answer is pending, or during a judged hold, the response is a wait; nothing it
    returns turns a wait into entry. A late, invalid or over-budget answer leaves
    the Rules action. ``max_added_wait_s`` bounds the judge-caused wait, summed
    over runs of pending/hold responses. A Rules wait (pad not clear) closes the
    current run; its duration is Rules time and is not charged to the judge.
    """

    def __init__(self, root, config, clock=time.monotonic):
        self.clock = clock
        self.config = config
        self.policy = config["world"]["pad_queue"]["mission_judge"]
        self.root = Path(root) / "pad-judge"
        self.issued = 0
        self.pending = None
        self.hold = None
        self.spent = 0.0  # judge-caused seconds from closed runs
        self.run_start = None  # wall time the current judge-caused run began
        self.run_start_monotonic = None
        self.released_wait_s = None

    def _now(self, request):
        observed = request["observations"][-1]["wall_s"]
        if self.run_start is None:
            return observed
        return max(observed, self.run_start + self.clock() - self.run_start_monotonic)

    def _added(self, now):
        return self.spent + (0.0 if self.run_start is None else now - self.run_start)

    def _expired(self, now):
        elapsed = max(
            now - self.pending["issued_wall_s"],
            self.clock() - self.pending["issued_monotonic_s"],
        )
        return elapsed > self.policy["judge_timeout_s"]

    def respond(self, request, response):
        started = self.clock()
        prior = response["proposed_action"]
        now = self._now(request)
        receipt = dict(prior_action=prior, mode=self.policy["mode"])
        if prior != ENTER:
            # The pad is not ready: close the judge-caused run and drop any judgment,
            # which belonged to another clear episode.
            if self.run_start is not None:
                self.spent += now - self.run_start
                self.run_start = None
            self.pending = self.hold = None
        receipt["added_wait_s"] = (
            self.released_wait_s if self.released_wait_s is not None else self._added(now)
        )
        if prior != ENTER:
            return self._final(response, receipt, "not_consulted", now, started)
        if self.released_wait_s is not None:
            return self._final(response, receipt, "decisions_exhausted", now, started)
        # Reserve the existing mailbox freshness allowance for executor release.
        release_at = (
            self.policy["max_added_wait_s"]
            - self.config["world"]["pad_queue"]["response_max_age_s"]
        )
        if receipt["added_wait_s"] >= release_at:
            self.pending = self.hold = None
            return self._final(response, receipt, "budget_exhausted", now, started)
        if self.hold and now < self.hold["until_wall_s"]:
            return self._final(response, dict(receipt, **self.hold["ref"]), "hold", now, started)
        self.hold = None
        if self.pending:
            ref = dict(judge_request_id=self.pending["request"]["judge_request_id"])
            if self._expired(now):
                self.pending = None
                return self._final(response, dict(receipt, **ref), "unavailable", now, started)
            path = self.pending["folder"] / "response.json"
            if not path.exists():
                return self._final(response, dict(receipt, **ref), "pending", now, started)
            try:
                answer = json.loads(path.read_text())
            except (ValueError, OSError):
                answer = None
            now = self._now(request)
            receipt["added_wait_s"] = self._added(now)
            if self._expired(now):
                self.pending = None
                return self._final(response, dict(receipt, **ref), "unavailable", now, started)
            if receipt["added_wait_s"] >= release_at:
                self.pending = None
                return self._final(response, dict(receipt, **ref), "budget_exhausted", now, started)
            decision = validate_judgment(self.pending["request"], answer)
            self.pending = None
            ref["judgment_sha256"] = digest(answer)
            if decision is None:
                status = (
                    "unavailable"
                    if isinstance(answer, dict) and answer.get("judge_status") == "unavailable"
                    else "invalid"
                )
                return self._final(response, dict(receipt, **ref), status, now, started)
            if decision["action"] == "wait":
                remaining = self.policy["max_added_wait_s"] - receipt["added_wait_s"]
                self.hold = dict(
                    until_wall_s=now + min(decision["wait_seconds"], remaining), ref=ref
                )
                return self._final(response, dict(receipt, **ref), "hold", now, started)
            return self._final(response, dict(receipt, **ref), "no_objection", now, started)
        if self.issued >= self.policy["max_decisions"]:
            return self._final(response, receipt, "decisions_exhausted", now, started)
        ref = self._issue(request, response, now, receipt["added_wait_s"])
        return self._final(response, dict(receipt, **ref), "pending", now, started)

    def _final(self, response, receipt, status, now, started):
        if status in JUDGE_WAITS and self.run_start is None:
            self.run_start, self.run_start_monotonic = now, started
        if self.run_start is not None:
            # Only an active judge-caused run carries an aircraft release deadline.
            receipt["wait_deadline_wall_s"] = (
                self.run_start + self.policy["max_added_wait_s"] - self.spent
            )
            if receipt["prior_action"] == ENTER and status in JUDGE_PASSES:
                self.released_wait_s = receipt["added_wait_s"]
        receipt["status"] = status
        return dict(response, **judge_overlay(self.policy, receipt["prior_action"], receipt))

    def _issue(self, request, response, now, waited):
        issued_monotonic = self.clock()
        sequence = self.issued
        self.issued += 1
        folder = self.root / f"{sequence:03d}"
        folder.mkdir(parents=True)
        judge_request = dict(
            schema="missionos.yokohama-pad-judge-request.v1",
            run_id=self.config["run_id"],
            sequence=sequence,
            observation_id=f"pad_judge_{sequence + 1}",
            pad_request_id=request["request_id"],
            issued_wall_s=now,
            situation=judge_situation(self.config, request, response),
            remaining_wait_seconds=int(self.policy["max_added_wait_s"] - waited),
            decisions_remaining=self.policy["max_decisions"] - self.issued,
            allowed_actions=["enter", "wait"],
            authority="Rules already allow entry; the judge may only add a bounded wait",
        )
        judge_request["judge_request_id"] = digest(judge_request)
        atomic_json(folder / "request.json", judge_request)
        self.pending = dict(
            request=judge_request,
            folder=folder,
            issued_wall_s=now,
            issued_monotonic_s=issued_monotonic,
        )
        if self.policy["mode"] == "fixture":
            atomic_json(folder / "response.json", fixture_judgment(judge_request))
        return dict(judge_request_id=judge_request["judge_request_id"])


class PadSupervisor:
    """Host-side MissionOS fixture. Commands remain on the aircraft side."""

    def __init__(self, root, config):
        self.root, self.config = Path(root), config
        self.advisory = None
        if config["world"].get("pad_state_advisory"):
            from scripts.yokohama_pad_advisory_host import PadAdvisoryHost

            self.advisory = PadAdvisoryHost(root, config)
        self.judge = None
        if config["world"]["pad_queue"].get("mission_judge"):
            self.judge = MissionJudgeGate(root, config)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self):
        sequence = 0
        try:
            while not self.stop_event.wait(0.02):
                if self.advisory and (self.root / "pad-advisory-closed.json").exists():
                    self.advisory.close()
                folder = self.root / "pad-decisions" / f"{sequence:03d}"
                path = folder / "request.json"
                if not path.exists():
                    continue
                request = json.loads(path.read_text())
                if request["sequence"] != sequence:
                    raise ValueError("Out-of-order pad request")
                response = propose(self.config, request)
                if self.advisory:
                    if self.advisory.closed:
                        raise ValueError("Pad request after advisory exit")
                    response = self.advisory.respond(request, folder, response)
                    if (self.root / "pad-advisory-closed.json").exists():
                        self.advisory.close()
                        raise ValueError("Late pad response after advisory exit")
                if self.judge:
                    response = self.judge.respond(request, response)
                atomic_json(folder / "response.json", response)
                sequence += 1
        except Exception as exc:
            atomic_json(self.root / "pad-supervisor-error.json", dict(error=str(exc)))

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("Pad supervisor did not stop")
        if self.advisory:
            self.advisory.close()
