"""Bounded background measurement, completely separate from flight control sampling."""

import json
import math
from pathlib import Path
import re
import subprocess
import threading
import time

TOPICS = (
    "vehicle_local_position",
    "vehicle_local_position_setpoint",
    "trajectory_setpoint",
    "vehicle_global_position",
    "home_position",
    "vehicle_local_position_groundtruth",
)
REQUIRED = TOPICS[:4]  # home_position is a static reference, not a dynamic cohort timestamp


def number(raw, name):
    hit = re.search(r"^\s*" + re.escape(name) + r":\s*([^\s,]+)", raw, re.M)
    try:
        value = float(hit.group(1)) if hit else None
        return value if value is not None and math.isfinite(value) else None
    except ValueError:
        return None


def boolean(raw, name):
    # PX4 listener uses True/False; legacy fixtures also use true/false or 1/0.
    hit = re.search(r"^\s*" + re.escape(name) + r":[ \t]*([^\s]+)[ \t]*$", raw, re.M)
    return bool(hit and hit.group(1).lower() in {"true", "1"})


def source_age(raw):
    # PX4 listener reports age from its own boot clock, not a host clock conversion.
    hit = re.search(r"^\s*timestamp:\s*\d+\s*\(([0-9.+eE-]+) seconds ago\)", raw, re.M)
    try:
        value = float(hit.group(1)) if hit else None
        return value if value is not None and math.isfinite(value) else None
    except ValueError:
        return None


def measurements(raw, before, after, phases, begin, end, config):
    local, controller = raw.get(TOPICS[0], ""), raw.get(TOPICS[1], "")
    z, target_z = number(local, "z"), number(controller, "z")
    vector = re.search(r"^\s*position:\s*\[([^]]+)\]", raw.get(TOPICS[2], ""), re.M)
    try:
        trajectory_z = float(vector.group(1).split(",")[2]) if vector else None
        trajectory_z = (
            trajectory_z if trajectory_z is not None and math.isfinite(trajectory_z) else None
        )
    except (ValueError, IndexError):
        trajectory_z = None
    global_alt, home_alt = (
        number(raw.get(TOPICS[3], ""), "alt"),
        number(raw.get(TOPICS[4], ""), "alt"),
    )
    ref_alt = number(local, "ref_alt")
    home_valid = boolean(raw.get(TOPICS[4], ""), "valid_alt")
    global_z = boolean(local, "z_global")
    relative = (
        global_alt - home_alt
        if home_valid and global_alt is not None and home_alt is not None
        else None
    )
    target_relative = (
        ref_alt - target_z - home_alt
        if global_z and home_valid and all(x is not None for x in (ref_alt, target_z, home_alt))
        else None
    )
    stamps = {t: number(raw.get(t, ""), "timestamp") for t in TOPICS}
    needed = [stamps[t] for t in REQUIRED]
    spread = max(needed) - min(needed) if all(x is not None and x > 0 for x in needed) else None
    a, b = before.get("sim_s"), after.get("sim_s")
    width = b - a if a is not None and b is not None else None
    pose = after.get("poses", {}).get("x500_0")
    age = pose.get("age_s") if pose else None
    sensor_sim = pose.get("sensor_sim_s") if pose else None
    sensor_age = b - sensor_sim if b is not None and sensor_sim is not None else None
    ages = {t: source_age(raw.get(t, "")) for t in TOPICS}
    # Include time spent collecting later topics, conservatively in host seconds.
    # No PX4 boot timestamp is converted to host Unix or Gazebo time.
    worst_ages = {t: x + end - begin if x is not None else None for t, x in ages.items()}
    fresh = (
        phases[0] == phases[1]
        and 0 <= end - begin <= 2
        and width is not None
        and 0 <= width <= 2
        and age is not None
        and 0 <= age <= 2
        and sensor_age is not None
        and 0 <= sensor_age <= 2
        and all(x is not None and 0 <= x for x in (ages[t] for t in REQUIRED))
        and all(x is not None and 0 <= x <= 2 for x in (worst_ages[t] for t in REQUIRED))
        and spread is not None
        and 0 <= spread <= 2e6
        and all(x is not None for x in (z, target_z, trajectory_z, relative, target_relative))
    )
    configured = next(
        (s["target_world_xyz_m"][2] for s in config["flight_stages"] if s["name"] == phases[1]),
        None,
    )
    return dict(
        schema="missionos.altitude-diagnostics.v1",
        run_id=config["run_id"],
        world_sha256=config["world"]["world_sha256"],
        phase=phases[1],
        phase_before=phases[0],
        capture_begin_host_monotonic_s=begin,
        capture_end_host_monotonic_s=end,
        capture_width_host_s=end - begin,
        sim_before_s=a,
        sim_after_s=b,
        capture_width_sim_s=width,
        clock_domains={
            "uorb": "PX4 boot microseconds",
            "sim": "Gazebo seconds",
            "host": "collector monotonic seconds; never Unix/worker elapsed",
        },
        simultaneous=False,
        fresh_for_comparison=bool(fresh),
        topic_timestamps_us=stamps,
        topic_listener_source_age_s=ages,
        topic_source_age_upper_bound_at_capture_end_s=worst_ages,
        gazebo_sensor_age_sim_s=sensor_age,
        required_topic_spread_us=spread,
        raw_px4=raw,
        gazebo_before=before.get("poses", {}).get("x500_0"),
        gazebo_after=pose,
        px4_estimated_local_ned_z_m=z,
        controller_setpoint_local_ned_z_m=target_z,
        trajectory_setpoint_local_ned_z_m=trajectory_z,
        px4_local_groundtruth_ned_z_m=number(raw.get(TOPICS[5], ""), "z"),
        px4_estimated_relative_home_altitude_m=relative,
        controller_target_relative_home_altitude_m=target_relative,
        px4_local_ref_alt_amsl_m=ref_alt,
        px4_home_alt_amsl_m=home_alt,
        local_ref_timestamp=number(local, "ref_timestamp"),
        z_reset_counter=number(local, "z_reset_counter"),
        configured_stage_world_z_m=configured,
        frame_note="Gazebo ENU world Z; PX4 local NED down; relative-home target uses observed reference and valid home",
    )


class AltitudeCollector:
    def __init__(
        self,
        obs,
        config,
        phase,
        path,
        *,
        prefix="/opt/px4-gazebo/bin/px4-",
        timeout=2,
        interval=0.3,
    ):
        self.obs, self.config, self.phase, self.path = obs, config, phase, Path(path)
        self.prefix, self.timeout, self.interval = prefix, timeout, interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="altitude-collector", daemon=False)
        self.cache = None
        self.process = None
        self.error = None

    def start(self):
        self.thread.start()

    def _listen(self, topic):
        proc = subprocess.Popen(
            [self.prefix + "listener", topic, "-n", "1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.process = proc
        deadline = time.monotonic() + (
            min(self.timeout, 0.1) if topic == TOPICS[5] else self.timeout
        )
        try:
            while not self.stop_event.is_set() and time.monotonic() < deadline:
                try:
                    out, _ = proc.communicate(timeout=0.05)
                    return out if proc.returncode == 0 else ""
                except subprocess.TimeoutExpired:
                    pass
            return ""
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=0.3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=0.5)
            self.process = None

    def _loop(self):
        try:
            with self.path.open("w", buffering=1) as out:
                while not self.stop_event.is_set():
                    begin, phase_before, before = (
                        time.monotonic(),
                        self.phase(),
                        self.obs.snapshot(),
                    )
                    raw, intervals = {}, {}
                    for topic in TOPICS:
                        if self.stop_event.is_set():
                            break
                        t = time.monotonic()
                        raw[topic] = self._listen(topic)
                        intervals[topic] = dict(
                            begin_host_monotonic_s=t,
                            end_host_monotonic_s=time.monotonic(),
                            available=bool(raw[topic]),
                        )
                    after, phase_after, end = self.obs.snapshot(), self.phase(), time.monotonic()
                    row = measurements(
                        raw, before, after, (phase_before, phase_after), begin, end, self.config
                    )
                    row["topic_capture_intervals"] = intervals
                    out.write(json.dumps(row, allow_nan=False) + "\n")
                    self.cache = row
                    if self.stop_event.wait(self.interval):
                        break
        except Exception as exc:
            self.error = type(exc).__name__

    def latest(self):
        row = self.cache
        if row is None:
            return None
        result = dict(row)
        age = time.monotonic() - row["capture_end_host_monotonic_s"]
        result["cache_age_host_s"] = age
        result["fresh_for_comparison"] = bool(row["fresh_for_comparison"] and 0 <= age <= 2)
        return result

    def close(self):
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=2)
        if self.thread.is_alive() or self.process is not None:
            raise RuntimeError("Altitude collector failed cleanup")
