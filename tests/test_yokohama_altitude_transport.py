"""Offline transport checks, including the actual generated MAVLink uploader."""

import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.smoke_px4_gazebo_sitl_mission_upload import _inner_upload_script
from scripts.yokohama_altitude_contract import (
    SCHEMA,
    AltitudeOffsetChanged,
    AltitudeReferenceChanged,
    compile_mission,
    digest,
    recheck_mapping,
    require_reference,
    verify_mapping,
    verify_transport_evidence,
    mission_wire_items,
)
from scripts.yokohama_flight_worker import prepare_altitude_upload


def observation():
    raw = dict(
        vehicle_local_position="""TOPIC: vehicle_local_position
 timestamp: 799020000 (0.004 seconds ago)
 z: -15.09643
 ref_alt: 0.70194
 ref_timestamp: 3524000
 z_reset_counter: 2
 z_global: True
 z_valid: True
""",
        vehicle_global_position="""TOPIC: vehicle_global_position
 timestamp: 799024000 (0.004 seconds ago)
 alt: 15.79837
 alt_valid: True
 alt_reset_counter: 2
""",
        home_position="""TOPIC: home_position
 timestamp: 10184000 (788.84 seconds ago)
 alt: 0.71249
 z: -0.01055
 update_count: 146
 valid_alt: True
""",
    )
    return dict(
        run_id="run",
        world_sha256="world",
        phase="PAYLOAD-CLIMB",
        wall_s=10.03,
        sim_s=799.06,
        altitude_capture=dict(begin_worker_wall_s=10, end_worker_wall_s=10.03, sim_before_s=799),
        raw_px4=raw,
        px4_relative_altitude_m=15.08588,
        vehicle=dict(xyz=[163, -65, 15.542793932990946], age_s=0.01, sensor_sim_s=799.052),
    )


def items(z=15.110143087104275):
    first = dict(
        seq=0,
        command=16,
        latitude_deg=35.447,
        longitude_deg=139.646,
        world_z_m=z,
        current=1,
        frame=6,
        param2=0.5,
    )
    return [first, dict(first, seq=1, command=17, current=0)]


def mapping(row=None, world_items=None, name="PAYLOAD-CLIMB"):
    return compile_mission(
        world_items or items(),
        row or observation(),
        segment=name,
        run_id="run",
        world_sha256="world",
        now_worker_wall_s=10.04,
    )


def test_measured_climb_offset_not_home_constant_or_setting_copy():
    row = observation()
    result = mapping(row)
    assert result["mission_items"][0][4] == pytest.approx(14.653229154113328)
    assert result["mission_items"][0][4] != pytest.approx(15.110143087104275 - 0.2998432)
    assert result["role"].startswith("planned")
    assert result["observation_sha256"] == digest(row)
    assert verify_mapping(result) == require_reference(row)
    assert recheck_mapping(result, row, 10.04) == result["reference"]
    # Static home can be old; it is not a dynamic telemetry sample.
    assert result["reference"]["origin"]["home_alt_m"] == 0.71249


@pytest.mark.parametrize(
    "fault",
    [
        "missing_capture",
        "missing_home",
        "missing_global",
        "home_invalid",
        "local_invalid",
        "global_invalid",
        "missing_age",
        "stale_topic",
        "negative_topic_age",
        "stale_pose",
        "future_pose",
        "reversed_sim",
        "reversed_host",
        "slow_capture",
        "topic_clock_spread",
        "wrong_local_reference",
        "wrong_home_local",
        "forged_relative",
        "forged_elapsed",
        "bool_relative",
    ],
)
def test_bad_observation_fails_before_compile(fault):
    row = observation()
    raw = row["raw_px4"]
    if fault == "missing_capture":
        del row["altitude_capture"]
    elif fault == "missing_home":
        del raw["home_position"]
    elif fault == "missing_global":
        del raw["vehicle_global_position"]
    elif fault == "home_invalid":
        raw["home_position"] = raw["home_position"].replace("True", "False")
    elif fault == "local_invalid":
        raw["vehicle_local_position"] = raw["vehicle_local_position"].replace("True", "False")
    elif fault == "global_invalid":
        raw["vehicle_global_position"] = raw["vehicle_global_position"].replace("True", "False")
    elif fault == "missing_age":
        raw["vehicle_global_position"] = raw["vehicle_global_position"].replace(
            "(0.004 seconds ago)", ""
        )
    elif fault == "stale_topic":
        raw["vehicle_global_position"] = raw["vehicle_global_position"].replace(
            "0.004 seconds", "2 seconds"
        )
    elif fault == "negative_topic_age":
        raw["vehicle_global_position"] = raw["vehicle_global_position"].replace(
            "0.004 seconds", "-0.010 seconds"
        )
    elif fault == "stale_pose":
        row["vehicle"]["age_s"] = 2.01
    elif fault == "future_pose":
        row["vehicle"]["sensor_sim_s"] = 802
    elif fault == "reversed_sim":
        row["altitude_capture"]["sim_before_s"] = 800
    elif fault == "reversed_host":
        row["altitude_capture"]["begin_worker_wall_s"] = 11
    elif fault == "slow_capture":
        row["altitude_capture"]["begin_worker_wall_s"] = 7
    elif fault == "topic_clock_spread":
        raw["vehicle_global_position"] = raw["vehicle_global_position"].replace(
            "799024000", "900000000"
        )
    elif fault == "wrong_local_reference":
        raw["vehicle_local_position"] = raw["vehicle_local_position"].replace("0.70194", "1.70194")
    elif fault == "wrong_home_local":
        raw["home_position"] = raw["home_position"].replace("-0.01055", "-0.1")
    elif fault == "forged_relative":
        row["px4_relative_altitude_m"] += 0.1
    elif fault == "forged_elapsed":
        row["wall_s"] += 0.1
    else:
        row["px4_relative_altitude_m"] = True
    with pytest.raises(ValueError):
        mapping(row)


@pytest.mark.parametrize("now", [9, 12.04, float("inf"), float("nan"), True])
def test_worker_elapsed_deadline_not_unix_or_touch(now):
    with pytest.raises(ValueError):
        require_reference(observation(), now)


@pytest.mark.parametrize("z", [None, True, float("nan"), float("inf")])
def test_invalid_world_height_denied(z):
    with pytest.raises(ValueError):
        mapping(world_items=items(z))


@pytest.mark.parametrize(
    "fault", ["frame", "seq", "missing_world_z", "nan_longitude", "boolean_seq"]
)
def test_invalid_mission_template_denied(fault):
    world = items()
    if fault == "frame":
        world[0]["frame"] = 0
    elif fault == "seq":
        world[1]["seq"] = 2
    elif fault == "missing_world_z":
        del world[0]["world_z_m"]
    elif fault == "nan_longitude":
        world[0]["longitude_deg"] = float("nan")
    else:
        world[0]["seq"] = False
    with pytest.raises(ValueError):
        mapping(world_items=world)


@pytest.mark.parametrize("fault", ["reset", "rehome", "offset", "cross_run", "stale", "tamper"])
def test_recheck_before_activation_rejects_changed_reference(fault):
    original = mapping()
    row = observation()
    if fault == "reset":
        row["raw_px4"]["vehicle_local_position"] = row["raw_px4"]["vehicle_local_position"].replace(
            "z_reset_counter: 2", "z_reset_counter: 3"
        )
        error = AltitudeReferenceChanged
    elif fault == "rehome":
        row["raw_px4"]["home_position"] = row["raw_px4"]["home_position"].replace(
            "10184000", "10188000"
        )
        error = AltitudeReferenceChanged
    elif fault == "offset":
        row["vehicle"]["xyz"][2] += 0.051
        error = AltitudeOffsetChanged
    elif fault == "cross_run":
        row["run_id"] = "other"
        error = ValueError
    elif fault == "stale":
        row["vehicle"]["age_s"] = 3
        error = ValueError
    else:
        original["mission_items"][0][4] += 0.1
        error = ValueError
    with pytest.raises(error):
        recheck_mapping(original, row, 10.04)


def test_all_phase_templates_preserve_world_goal_payload_and_return(tmp_path):
    names = [
        "SEA-TAKEOFF",
        "00-D1",
        "01-D2",
        "02-D3",
        "03-DELIVERY",
        "PAYLOAD-LOW",
        "PAYLOAD-CLIMB",
        "04-D3",
        "05-D2",
        "06-D1",
        "SEA-OUTBOUND-COAST",
        "SEA-RETURN",
    ]
    cfg = dict(
        run_id="run",
        world=dict(world_sha256="world"),
        flight_stages=[
            dict(
                name=name,
                target_world_xyz_m=[
                    163,
                    -65,
                    2.792366302986944 if name == "PAYLOAD-LOW" else 15.110143087104275,
                ],
                items=items(2.792366302986944 if name == "PAYLOAD-LOW" else 15.110143087104275),
            )
            for name in names
        ],
    )
    snapshot = copy.deepcopy(cfg)
    for stage in cfg["flight_stages"]:
        row = observation()
        row["phase"] = stage["name"]
        result = prepare_altitude_upload(tmp_path, cfg, stage["name"], row, 10.04)
        command = result["mission_items"][0][4]
        # Invert the transport transform; approval, geometry, parcel receiver
        # and independent hold targets remain in the original world frame.
        assert command + result["reference"]["world_minus_relative_m"] == pytest.approx(
            stage["target_world_xyz_m"][2]
        )
    assert cfg == snapshot
    # A connector must remap at actual dispatch, not retain the prior city offset.
    (tmp_path / "city-connect-world-items.json").write_text(json.dumps(items()))
    row = observation()
    row["vehicle"]["xyz"][2] += 0.1
    import hashlib

    connector_hash = hashlib.sha256(
        (tmp_path / "city-connect-world-items.json").read_bytes()
    ).hexdigest()
    result = prepare_altitude_upload(
        tmp_path, cfg, "city-connect", row, 10.04, expected_world_items_sha256=connector_hash
    )
    (tmp_path / "city-connect-world-items.json").write_text(json.dumps(items(99)))
    with pytest.raises(ValueError, match="permit"):
        prepare_altitude_upload(
            tmp_path, cfg, "city-connect", row, 10.04, expected_world_items_sha256=connector_hash
        )
    assert result["mission_items"][0][4] == pytest.approx(mapping()["mission_items"][0][4] - 0.1)


DRIVER = """import json, runpy, socket, struct, sys
sent=[]; replies=[]
def reply(mid,payload):
 return bytes([253,len(payload),0,0,0,1,1,mid,0,0])+payload+b"\\0\\0"
class FakeSocket:
 def __enter__(self): return self
 def __exit__(self,*args): pass
 def setsockopt(self,*args): pass
 def settimeout(self,*args): pass
 def bind(self,*args): pass
 def sendto(self,packet,address):
  mid=packet[7]; payload=packet[10:10+packet[1]]
  if mid==45: replies.append(reply(47,b"\\1\\1\\0"))
  if mid==44:
   count=struct.unpack("<H",payload[:2])[0]
   replies.extend(reply(51,struct.pack("<H",i)) for i in range(count))
   replies.append(reply(47,b"\\1\\1\\0"))
  if mid==73:
   fields=struct.unpack("<ffffiifHHBBBBBB",payload)
   sent.append(dict(seq=fields[7],altitude_m=fields[6],frame=fields[11]))
 def recvfrom(self,*args):
  if not replies: raise socket.timeout()
  return replies.pop(0),("127.0.0.1",0)
socket.socket=lambda *a,**k: FakeSocket()
runpy.run_path(sys.argv[1],run_name="__main__")
print(json.dumps(dict(fixture_wire_packets=sent)))
"""


def test_real_uploader_subprocess_consumes_only_runtime_mapped_sidecar(tmp_path):
    cfg = dict(
        run_id="run",
        world=dict(world_sha256="world"),
        flight_stages=[dict(name="PAYLOAD-CLIMB", items=items())],
    )
    result = prepare_altitude_upload(tmp_path, cfg, "PAYLOAD-CLIMB", observation(), 10.04)
    uploader = tmp_path / "upload.py"
    uploader.write_text(
        _inner_upload_script(
            reuse_mavlink_session=True,
            runtime_items_path=str(tmp_path / "PAYLOAD-CLIMB-altitude-items.json"),
        )
    )
    driver = tmp_path / "fake-px4-upload.py"
    driver.write_text(DRIVER)
    proc = subprocess.run(
        [sys.executable, str(driver), str(uploader)], capture_output=True, text=True, timeout=5
    )
    assert proc.returncode == 0, proc.stderr
    receipt, wire = [json.loads(x) for x in proc.stdout.splitlines()]
    assert receipt["mission_ack_type"] == 0
    assert receipt["mission_items"] == result["mission_items"]
    assert all(
        x["frame"] == 6
        and x["altitude_m"] == pytest.approx(result["mission_items"][0][4], abs=1e-6)
        for x in wire["fixture_wire_packets"]
    )
    (tmp_path / "PAYLOAD-CLIMB-altitude-items.json").unlink()
    missing = subprocess.run(
        [sys.executable, str(driver), str(uploader)], capture_output=True, text=True, timeout=5
    )
    assert missing.returncode != 0 and not missing.stdout


def evidence():
    cfg = dict(
        run_id="run",
        world=dict(world_sha256="world"),
        altitude_transport_contract=SCHEMA,
        flight_stages=[dict(name="PAYLOAD-CLIMB", items=items())],
    )
    mapped = mapping()
    identity = digest(mapped)
    events = [
        dict(
            event="altitude_transport_prepared",
            mapping=mapped,
            mapping_sha256=identity,
            segment="PAYLOAD-CLIMB",
            phase="PAYLOAD-CLIMB",
            wall_s=10.04,
        ),
        dict(
            event="upload_receipt",
            segment="PAYLOAD-CLIMB",
            wall_s=10.05,
            receipt=dict(mission_ack_type=0, mission_items=mapped["mission_items"]),
        ),
        dict(
            event="altitude_transport_command_sent",
            segment="PAYLOAD-CLIMB",
            phase="PAYLOAD-CLIMB",
            wall_s=10.07,
            dispatched_at_worker_wall_s=10.06,
            mapping_sha256=identity,
            observation=observation(),
        ),
    ]
    return cfg, events


def test_verifier_preserves_legacy_and_requires_new_binding(tmp_path):
    cfg, events = evidence()
    assert verify_transport_evidence(tmp_path, cfg, events)["status"] == "passed"
    assert verify_transport_evidence(tmp_path, {}, [])["status"] == "legacy"


def test_recovery_phase_commands_existing_approved_stage_without_rewriting_evidence(tmp_path):
    cfg, events = evidence()
    row = observation()
    row["phase"] = "recovery_return"
    mapped = mapping(row)
    events[0].update(mapping=mapped, mapping_sha256=digest(mapped), phase="recovery_return")
    events[2].update(mapping_sha256=digest(mapped), observation=row, phase="recovery_return")
    snapshot = copy.deepcopy(events)
    assert verify_transport_evidence(tmp_path, cfg, events)["status"] == "passed"
    assert events == snapshot


def test_city_phase_label_cannot_substitute_missing_approved_stage_command(tmp_path):
    cfg, events = evidence()
    mapped = mapping(name="city-01")
    identity = digest(mapped)
    events[0].update(mapping=mapped, mapping_sha256=identity, segment="city-01")
    events[1]["segment"] = "city-01"
    events[2].update(mapping_sha256=identity, segment="city-01")
    events.append(dict(event="city_permit_consumed", permit=dict(
        upload_name="city-01", connector_name="city-exit", altitude_transport_sha256=identity,
        candidate=dict(target_world_xyz_m=[0, 0, items()[0]["world_z_m"]]))))
    with pytest.raises(ValueError, match="Missing altitude mission phase"):
        verify_transport_evidence(tmp_path, cfg, events)


@pytest.mark.parametrize(
    "fault",
    [
        "missing_command",
        "wrong_world_goal",
        "wrong_receipt",
        "wrong_hash",
        "expired",
        "wrong_phase",
        "wrong_order",
        "unknown_contract",
    ],
)
def test_transport_verifier_denies_unbound_evidence(tmp_path, fault):
    cfg, events = copy.deepcopy(evidence())
    if fault == "missing_command":
        events.pop()
    elif fault == "wrong_world_goal":
        cfg["flight_stages"][0]["items"][0]["world_z_m"] += 1
    elif fault == "wrong_receipt":
        events[1]["receipt"]["mission_items"] = copy.deepcopy(events[1]["receipt"]["mission_items"])
        events[1]["receipt"]["mission_items"][0][4] += 1
    elif fault == "wrong_hash":
        events[-1]["mapping_sha256"] = "unbound"
    elif fault == "expired":
        events[-1]["dispatched_at_worker_wall_s"] = 13
        events[-1]["wall_s"] = 13.1
    elif fault == "wrong_phase":
        events[-1]["phase"] = "other"
    elif fault == "wrong_order":
        events[-1]["dispatched_at_worker_wall_s"] = 10.01
    else:
        cfg["altitude_transport_contract"] = "other"
    with pytest.raises(ValueError):
        verify_transport_evidence(tmp_path, cfg, events)


@pytest.mark.parametrize("prepared_protocol", [False, True])
def test_production_worker_rebinds_armed_home_then_dispatches_and_returns_offline(
    tmp_path, monkeypatch, prepared_protocol
):
    """Real worker boundary with a deterministic AP/Gazebo double, not a flight."""
    from types import SimpleNamespace
    from scripts import yokohama_flight_worker as worker
    from scripts.yokohama_altitude_contract import field as numeric, valid

    class Fixture:
        t = 10.0
        armed = 1
        landed = True
        nav = 4
        home_alt = 0.66603
        home_stamp = 1000000
        mission_id = 0
        mission_count = 0
        xyz = [0, 0, 0.2998432]
        images = {}
        camera_info = {}
        uploads = []

        def monotonic(self):
            self.t += 0.001
            return self.t

        def sleep(self, seconds):
            self.t += seconds

        @property
        def contacts(self):
            return [
                dict(topic="launch_pad", collision1="x500", collision2="pad", sensor_sim_s=self.t)
            ]

        def snapshot(self):
            return dict(
                sim_s=self.t,
                poses={
                    "x500_0": dict(
                        xyz=list(self.xyz), quat_wxyz=[1, 0, 0, 0], age_s=0.001, sensor_sim_s=self.t
                    )
                },
            )

        def images_to_disk(self, *args):
            return {}

        def record_motion(self, *args):
            pass

        def run(self, argv, timeout=None):
            command = Path(argv[0]).name
            if command == "python3":
                if Path(argv[1]).name == "upload-prepare.py":
                    tx = json.loads((tmp_path / "upload-transaction.json").read_text())
                    self.t += 3
                    return json.dumps(dict(schema="missionos.mavlink-upload-preparation.v1",
                        transaction_id=tx["transaction_id"], transaction_sha256=digest(tx),
                        clear_outcome="unconfirmed", clear_ack_type=None,
                        prepared_at_worker_wall_s=self.t - tx["worker_epoch_monotonic_s"],
                        mavlink_session_reused=tx["reuse_mavlink_session"]))
                name = Path(argv[1]).name.removesuffix("-upload.py")
                wire = json.loads((tmp_path / (name + "-altitude-items.json")).read_text())
                self.uploads.append(wire)
                self.mission_id += 1
                self.mission_count = len(wire)
                receipt = dict(mission_ack_type=0, mission_items=wire)
                if prepared_protocol:
                    bind = json.loads((tmp_path / (name + "-upload-binding.json")).read_text())
                    assert argv[2] == bind["transaction"]["transaction_id"]
                    receipt.update(mission_items_wire=mission_wire_items(wire), transaction_id=argv[2], transaction_sha256=digest(bind["transaction"]),
                                   mapping_sha256=bind["mapping_sha256"], preparation=bind["preparation"])
                return json.dumps(receipt)
            if command == "px4-commander":
                if argv[1] == "arm":
                    self.armed = 2
                    self.home_alt = 0.71249
                    self.home_stamp = 2000000
                elif argv[1] == "land":
                    self.landed = True
                    self.armed = 1
                    self.xyz = [0, 0, 0.2998432]
                elif argv[-1] == "auto:mission":
                    self.nav = 3
                    self.landed = False
                    self.xyz = [0, 0, self.uploads[-1][-1][4] + 0.2998432]
                elif argv[-1] == "auto:loiter":
                    self.nav = 4
                return ""
            if command != "px4-listener":
                return ""
            topic = argv[1]
            relative = self.xyz[2] - 0.2998432
            global_alt = relative + self.home_alt
            local_z = 0.70194 - global_alt
            data = {
                "vehicle_local_position": dict(
                    z=local_z,
                    x=0,
                    y=0,
                    vx=0,
                    vy=0,
                    vz=0,
                    z_valid=True,
                    z_global=True,
                    xy_valid=True,
                    ref_alt=0.70194,
                    ref_timestamp=500000,
                    z_reset_counter=2,
                    xy_reset_counter=0,
                    heading_reset_counter=0,
                    heading=0,
                ),
                "vehicle_global_position": dict(
                    alt=global_alt, alt_valid=True, alt_reset_counter=2
                ),
                "home_position": dict(
                    alt=self.home_alt,
                    z=0.70194 - self.home_alt,
                    valid_alt=True,
                    update_count=146 if self.armed == 1 else 147,
                ),
                "vehicle_status": dict(
                    pre_flight_checks_pass=True, arming_state=self.armed, nav_state=self.nav
                ),
                "vehicle_land_detected": dict(landed=self.landed),
                "mission_result": dict(
                    valid=True, mission_id=self.mission_id, seq_total=self.mission_count
                ),
                "battery_status": dict(remaining=0.9),
            }[topic]
            stamp = self.home_stamp if topic == "home_position" else int(self.t * 1e6)
            return f"timestamp: {stamp} (0.001 seconds ago)\n" + "".join(
                f"{k}: {v}\n" for k, v in data.items()
            )

    fake = Fixture()

    def parse(raw, key):
        if key in ("xy_valid", "pre_flight_checks_pass", "valid", "landed", "valid_alt"):
            return valid(raw, key)
        return numeric(raw, key)

    monkeypatch.setattr(worker, "ROOT", tmp_path)
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=fake.monotonic, sleep=fake.sleep))
    cfg = dict(
        run_id="run",
        world=dict(world_sha256="world"),
        altitude_transport_contract=SCHEMA,
        timeout_s=600,
        airspeed_mps=3,
        hold_duration_sim_s=30,
        hold_horizontal_tolerance_m=0.6,
        hold_vertical_tolerance_m=0.6,
        hold_max_speed_mps=0.5,
        flight_stages=[
            dict(name="00-D1", target_world_xyz_m=[0, 0, 15.110143087104275], items=items())
        ],
    )
    if prepared_protocol:
        cfg["mission_upload_preparation"] = "missionos.mavlink-upload-preparation.v1"
    result = worker.flight_trial(cfg, fake, fake.run, parse)
    assert result["status"] == "passed" and result["landing_on_launch_pad_observed"]
    assert len(fake.uploads) == 2  # one grounded pre-arm upload, one final home rebind
    events = [
        json.loads(line) for line in (tmp_path / "flight-events.jsonl").read_text().splitlines()
    ]
    assert sum(e["event"] == "altitude_home_rebind_before_takeoff" for e in events) == 1
    assert sum(e["event"] == "altitude_transport_command_sent" for e in events) == 1
    assert verify_transport_evidence(tmp_path, cfg, events)["status"] == "passed"
    assert result["holds"][0]["duration_sim_s"] >= 30
    assert result["holds"][0]["target_world_xyz_m"] == cfg["flight_stages"][0]["target_world_xyz_m"]


@pytest.mark.parametrize("kind", ["city_expiry", "pad_expiry", "valid"])
def test_final_command_deadline_after_observation_and_pad_check(kind):
    from scripts.yokohama_flight_worker import send_mapped_mission_command

    sent = []
    events = []
    checked = []
    times = iter([10.04, 10.10])
    expiry = 10.05 if kind == "city_expiry" else 10.20
    permission = dict(rules_checked_at=dict(wall_s=-19.95 if kind == "pad_expiry" else -19.80))

    def invoke():
        send_mapped_mission_command(
            lambda args: sent.append(args),
            lambda name, **value: events.append((name, value)),
            mapping(),
            observation(),
            lambda: next(times),
            expires_at_wall_s=expiry,
            pad_permission=permission,
            pad_check=lambda: checked.append("independent pad Rules"),
        )

    if kind == "valid":
        invoke()
        assert len(sent) == len(events) == 1 and checked
        assert events[0][1]["dispatched_at_worker_wall_s"] == 10.10
    else:
        with pytest.raises(ValueError, match="expired"):
            invoke()
        assert not sent and not events and checked


def test_runtime_template_file_cannot_override_approved_ap_world_goal(tmp_path):
    cfg = dict(
        run_id="run",
        world=dict(world_sha256="world"),
        flight_stages=[dict(name="PAYLOAD-CLIMB", items=items())],
    )
    (tmp_path / "PAYLOAD-CLIMB-world-items.json").write_text(json.dumps(items(999)))
    result = prepare_altitude_upload(tmp_path, cfg, "PAYLOAD-CLIMB", observation(), 10.04)
    assert result["world_items"] == cfg["flight_stages"][0]["items"]


@pytest.mark.parametrize("prepared_protocol", [False, True])
def test_city_and_connector_use_unified_mapping_without_sea_flag(tmp_path, monkeypatch, prepared_protocol):
    from scripts import yokohama_decision_host as module
    from scripts.yokohama_decision_host import DecisionHost

    row = observation()
    row.update(phase="00-D1", heading_ned_rad=0)
    candidate = dict(target_world_xyz_m=[168, -65, 15.110143087104275], target_heading_ned_rad=0)
    host = DecisionHost.__new__(DecisionHost)
    host.root = tmp_path
    host.bundle = tmp_path
    host.config = dict(
        run_id="run",
        world=dict(world_sha256="world", frame=dict(home_lon_lat=[139.6448, 35.4476])),
        altitude_transport_contract=SCHEMA,
        decisions=dict(),
        flight_stages=[
            dict(
                name="01-D2",
                target_world_xyz_m=[190, -65, 15.110143087104275],
                items=[dict(param4=0)],
            )
        ],
    )
    if prepared_protocol:
        host.config["mission_upload_preparation"] = "missionos.mavlink-upload-preparation.v1"
    host.pending = {
        1: dict(
            wam=dict(passed=True),
            input_observation=row,
            candidate=candidate,
            vla_response_sha256="vla",
            wam_assessment_sha256="wam",
        )
    }
    host.proposal_origin = lambda proposal: None
    host.pad_cycle = lambda cycle: False
    monkeypatch.setattr(
        module,
        "geometry_rules",
        lambda *a, **kw: dict(
            start_world_xyz_m=row["vehicle"]["xyz"],
            next_target_world_xyz_m=[190, -65, 15.110143087104275],
        ),
    )
    permit = host.authorize(
        dict(cycle=1, observation=row, next_target_world_xyz_m=[190, -65, 15.110143087104275]),
        tmp_path,
    )
    candidate_mapping = json.loads((tmp_path / "city-01-altitude-mapping.json").read_text())
    assert digest(candidate_mapping) == permit["altitude_transport_sha256"]
    assert candidate_mapping["mission_items"][0][4] == pytest.approx(
        mapping()["mission_items"][0][4]
    )
    templates = json.loads((tmp_path / "city-01-connect-world-items.json").read_text())
    assert all(i["world_z_m"] == pytest.approx(15.110143087104275) for i in templates)
    current = copy.deepcopy(row)
    current["vehicle"]["xyz"][2] += 0.1
    connector = prepare_altitude_upload(
        tmp_path,
        host.config,
        permit["connector_name"],
        current,
        10.04,
        expected_world_items_sha256=permit["connector_world_items_sha256"],
    )
    assert connector["mission_items"][0][4] == pytest.approx(
        candidate_mapping["mission_items"][0][4] - 0.1
    )
    with pytest.raises(AltitudeOffsetChanged):
        host.activate(
            dict(cycle=1, prepared_permit_sha256=digest(permit), observation=current), tmp_path
        )


class UploadDouble:
    """Run generated wire code with fake time/socket, no real communication."""
    def __init__(self, mode="missing", wall=100.0):
        self.wall = wall
        self.mode = mode
        self.trace = []
        self.outputs = []
        self.files = {}
        self.wire = []

    def execute(self, source, argv):
        import builtins
        import io
        import struct
        from types import SimpleNamespace

        double = self

        def packet(mid, payload):
            return bytes([253, len(payload), 0, 0, 0, 1, 1, mid, 0, 0]) + payload + b"\0\0"

        class Socket:
            timeout = 0
            phase = "none"
            request = 0
            count = 0

            def __enter__(self): return self
            def __exit__(self, *args): pass
            def setsockopt(self, *args): pass
            def bind(self, *args): pass
            def settimeout(self, value): self.timeout = value

            def sendto(self, data, address):
                mid, payload = data[7], data[10:10 + data[1]]
                double.trace.append((mid, double.wall))
                if mid == 45:
                    self.phase = "clear"
                if mid == 44:
                    self.phase = "send"
                    self.count = struct.unpack("<H", payload[:2])[0]
                if mid == 73:
                    decoded = struct.unpack("<ffffiifHHBBBBBB", payload)
                    double.wire.append(decoded)

            def recvfrom(self, *args):
                if self.phase == "clear":
                    if double.mode == "ack":
                        double.wall += .001
                        return packet(47, bytes([255, 190, 0])), ("fixture", 0)
                    if double.mode == "nack":
                        return packet(47, bytes([255, 190, 1])), ("fixture", 0)
                    double.wall += self.timeout
                    if double.mode == "silent":
                        raise TimeoutError()
                    return packet(0, b"\0" * 9), ("fixture", 0)
                double.wall += .001
                if self.request < self.count:
                    seq = self.request
                    self.request += 1
                    return packet(51, struct.pack("<H", seq) + bytes([255, 190])), ("fixture", 0)
                return packet(47, bytes([255, 190, 0])), ("fixture", 0)

        fake_socket = SimpleNamespace(socket=lambda *a, **k: Socket(), timeout=TimeoutError,
                                      AF_INET=2, SOCK_DGRAM=2, SOL_SOCKET=1, SO_REUSEADDR=2)
        real_import = builtins.__import__

        def forbidden(*args, **kwargs): raise AssertionError("No process/socket allowed")

        def importer(name, *args, **kwargs):
            if name == "socket":
                return fake_socket
            if name == "time":
                return SimpleNamespace(monotonic=lambda: self.wall,
                                       sleep=lambda seconds: setattr(self, "wall", self.wall + seconds))
            if name == "subprocess":
                return SimpleNamespace(run=forbidden, DEVNULL=-3)
            if name == "sys":
                return SimpleNamespace(argv=argv, exit=lambda code: (_ for _ in ()).throw(SystemExit(code)))
            return real_import(name, *args, **kwargs)

        def open_file(path, *args, **kwargs):
            return io.StringIO(json.dumps(self.files[path]))

        functions = dict(vars(builtins), __import__=importer, open=open_file,
                         print=lambda value, **kw: self.outputs.append(json.loads(value)))
        try:
            exec(compile(source, "actual-generated-uploader", "exec"), {"__builtins__": functions})
        except SystemExit as exc:
            assert exc.code == 0
        return self.outputs[-1]


def transaction():
    return dict(transaction_id="a" * 32, run_id="run", world_sha256="world",
                segment="PAYLOAD-CLIMB", reuse_mavlink_session=True,
                worker_epoch_monotonic_s=90.0)


@pytest.mark.parametrize("mode", ["missing", "silent", "ack"])
def test_prepare_then_fresh_mapping_avoids_recorded_three_second_drift(mode):
    from scripts.yokohama_altitude_contract import validate_upload_preparation

    double = UploadDouble(mode)
    tx = transaction()
    double.files["tx"] = tx
    prep = double.execute(_inner_upload_script(preparation_only_path="tx"), ["prepare"])
    validate_upload_preparation(tx, prep)
    expected = .001 if mode == "ack" else 3
    assert double.wall - 100 == pytest.approx(expected)
    assert all(mid != 44 for mid, _ in double.trace)
    original = observation()
    current = copy.deepcopy(original)
    current["vehicle"]["xyz"][2] += 0.056338257936154434
    current["altitude_capture"]["begin_worker_wall_s"] = double.wall - 90
    current["altitude_capture"]["end_worker_wall_s"] = double.wall - 90 + .03
    current["wall_s"] = current["altitude_capture"]["end_worker_wall_s"]
    double.wall += .03
    with pytest.raises(AltitudeOffsetChanged):
        recheck_mapping(mapping(original), current, current["wall_s"])
    fresh = compile_mission(items(), current, segment=tx["segment"], run_id="run",
                            world_sha256="world", now_worker_wall_s=current["wall_s"])
    double.files.update(wire=fresh["mission_items"],
                        binding=dict(transaction=tx, preparation=prep, mapping=fresh,
                                     mapping_sha256=digest(fresh)))
    receipt = double.execute(_inner_upload_script(runtime_items_path="wire", prepared_binding_path="binding"),
                             ["send", tx["transaction_id"]])
    assert receipt["mission_items"] == fresh["mission_items"]
    assert receipt["mission_items_wire"] == mission_wire_items(fresh["mission_items"])
    assert receipt["mapping_sha256"] == digest(fresh)
    assert receipt["preparation"] == prep
    assert receipt["mission_ack_type"] == 0
    assert sum(mid == 45 for mid, _ in double.trace) == 1
    assert double.wall - 90 - current["wall_s"] < .01
    assert double.wire[0][6] == pytest.approx(fresh["mission_items"][0][4], abs=1e-6)
    assert recheck_mapping(fresh, current, double.wall - 90)


def test_preparation_nack_cannot_produce_count_or_ready():
    double = UploadDouble("nack")
    double.files["tx"] = transaction()
    with pytest.raises(ValueError, match="Clear mission rejected"):
        double.execute(_inner_upload_script(preparation_only_path="tx"), ["prepare"])
    assert not double.outputs and not any(mid == 44 for mid, _ in double.trace)


@pytest.mark.parametrize("fault", ["nonce", "expiry", "mapping", "preparation"])
def test_prepared_send_rejects_stale_or_unbound_before_socket_send(fault):
    tx = transaction()
    double = UploadDouble("ack")
    double.files["tx"] = tx
    prep = double.execute(_inner_upload_script(preparation_only_path="tx"), ["prepare"])
    row = observation()
    row["altitude_capture"]["begin_worker_wall_s"] = 10.002
    row["altitude_capture"]["end_worker_wall_s"] = row["wall_s"] = 10.03
    fresh = mapping(row)
    double.wall = 100.04
    bind = dict(transaction=tx, preparation=prep, mapping=fresh, mapping_sha256=digest(fresh))
    nonce = tx["transaction_id"]
    if fault == "nonce":
        nonce = "b" * 32
    if fault == "expiry":
        double.wall += 3
    if fault == "mapping":
        bind["mapping_sha256"] = "wrong"
    if fault == "preparation":
        bind["preparation"]["transaction_sha256"] = "wrong"
    double.files.update(wire=fresh["mission_items"], binding=bind)
    trace_len = len(double.trace)
    with pytest.raises(ValueError):
        double.execute(_inner_upload_script(runtime_items_path="wire", prepared_binding_path="binding"),
                       ["send", nonce])
    assert len(double.trace) == trace_len


@pytest.mark.parametrize("fault", [None, "before_ready", "nonce", "wire", "ready_digest"])
def test_verifier_binds_preparation_fresh_capture_and_exact_wire(tmp_path, fault):
    from scripts.yokohama_altitude_contract import UPLOAD_PREPARATION_SCHEMA

    cfg, events = evidence()
    cfg["mission_upload_preparation"] = UPLOAD_PREPARATION_SCHEMA
    tx = transaction()
    prep = dict(schema=UPLOAD_PREPARATION_SCHEMA, transaction_id=tx["transaction_id"],
                transaction_sha256=digest(tx), prepared_at_worker_wall_s=9.99,
                clear_outcome="unconfirmed", clear_ack_type=None, mavlink_session_reused=True)
    protocol = dict(event="upload_protocol_prepared", phase="PAYLOAD-CLIMB", segment="PAYLOAD-CLIMB",
                    wall_s=9.995, transaction=tx, preparation=prep)
    events.insert(0, protocol)
    events[1]["transaction_id"] = tx["transaction_id"]
    receipt = events[2]["receipt"]
    receipt.update(transaction_id=tx["transaction_id"], transaction_sha256=digest(tx),
                   mapping_sha256=digest(events[1]["mapping"]), preparation=copy.deepcopy(prep),
                   mission_items_wire=mission_wire_items(receipt["mission_items"]))
    if fault == "before_ready":
        prep["prepared_at_worker_wall_s"] = 10.02
        receipt["preparation"] = copy.deepcopy(prep)
    if fault == "nonce":
        receipt["transaction_id"] = "old"
    if fault == "wire":
        receipt["mission_items_wire"][0][4] += .001
    if fault == "ready_digest":
        prep["transaction_sha256"] = "wrong"
    if fault:
        with pytest.raises(ValueError):
            verify_transport_evidence(tmp_path, cfg, events)
    else:
        assert verify_transport_evidence(tmp_path, cfg, events)["status"] == "passed"


@pytest.mark.parametrize("preparation_fails", [False, True])
def test_production_city_authorization_observes_after_protocol_preparation(tmp_path, preparation_fails):
    from scripts.yokohama_decision_worker import CityDecisions, attempt_name

    control = CityDecisions.__new__(CityDecisions)
    control.config = dict(run_id="run", decisions=dict(wam_profile="motion-v4"))
    control.root = tmp_path
    control.cycle, control.attempt = 0, 0
    control.started, control.active = False, False
    clock, trace = [10.03], []
    control.clock = lambda: clock[0]

    def sample():
        row = observation()
        row["wall_s"] = clock[0]
        return row

    control.sample = sample
    control.held = lambda *args, **kw: sample()
    control.event = lambda *args, **kw: None
    control.capture = lambda *args: dict(file="unused")
    control.before_authorize = lambda: trace.append("pad")

    def exchange(operation, row, **kw):
        if operation in ("start", "resume"):
            return {}
        if operation == "vla":
            return dict(vla_response_sha256="vla")
        if operation == "wam":
            return dict(passed=True)
        assert operation == "authorize"
        trace.append("authorize")
        assert row["wall_s"] > 13
        name = attempt_name(control.cycle, control.attempt)
        script = tmp_path / (name + "-upload.py")
        script.write_text("fixture")
        import hashlib
        return dict(run_id="run", config_sha256=digest(control.config), cycle=control.cycle,
                    observation_sha256=digest(row), expires_at_worker_wall_s=clock[0]+2,
                    upload_name=name, attempt=0, vla_response_sha256="vla",
                    wam_assessment_sha256=digest(dict(passed=True)), rules=dict(allowed=True),
                    upload_sha256=hashlib.sha256(script.read_bytes()).hexdigest())

    control.exchange = exchange

    def prepare(name):
        assert name == "city-01"
        trace.append("prepare")
        clock[0] += 3
        if preparation_fails:
            raise TimeoutError("fixture prepare")

    if preparation_fails:
        with pytest.raises(TimeoutError):
            control.decide(None, [1, 0, 15], prepare_upload=prepare)
        assert trace == ["prepare"]
    else:
        permit = control.decide(None, [1, 0, 15], prepare_upload=prepare)
        assert trace == ["prepare", "pad", "authorize"]
        assert permit["expires_at_worker_wall_s"] == pytest.approx(15.03)
