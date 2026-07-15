from __future__ import annotations

import math

from scripts import smoke_px4_gazebo_horizontal_route_delivery as route_entrypoint
from src.runtime.px4_gazebo_route import observation


CONTACT_TOPIC = "/mission_designer/collision_obstacle/contacts"


def test_legacy_route_entrypoint_uses_packaged_observation_functions() -> None:
    bindings = {
        "_battery_status_from_listener_output": (
            observation.battery_status_from_listener_output
        ),
        "_contact_topic_observation": observation.contact_topic_observation,
        "_distance_to_segment_xy": observation.distance_to_segment_xy,
        "_listener_field": observation.listener_field,
        "_point_to_segment_distance_m": observation.point_to_segment_distance_m,
        "_pose_rows": observation.pose_rows,
        "_select_contact_topic": observation.select_contact_topic,
        "_terminal_pose_summary_fields": observation.terminal_pose_summary_fields,
        "_xy_pairs_match": observation.xy_pairs_match,
    }
    for legacy_name, packaged_function in bindings.items():
        assert getattr(route_entrypoint, legacy_name) is packaged_function


def test_battery_listener_output_is_normalized_without_execution_claims() -> None:
    output = "\n".join(
        [
            "remaining: 0.425",
            "warning: 2",
            "voltage_v: 15.23456",
            "current_a: -3.5",
            "connected: True",
        ]
    )
    assert observation.battery_status_from_listener_output(
        output,
        returncode=0,
    ) == {
        "battery_status_observed": True,
        "battery_state_source": "px4-listener:battery_status",
        "battery_remaining_percent": 42.5,
        "battery_warning": 2,
        "battery_voltage_v": 15.235,
        "battery_current_a": -3.5,
        "battery_connected": True,
    }

    failed = observation.battery_status_from_listener_output(output, returncode=1)
    assert failed["battery_status_observed"] is False
    assert failed["battery_remaining_percent"] == 42.5

    missing = observation.battery_status_from_listener_output(
        "connected: False",
        returncode=0,
    )
    assert missing["battery_status_observed"] is False
    assert missing["battery_remaining_percent"] is None
    assert missing["battery_connected"] is False


def test_pose_rows_and_terminal_summary_preserve_observation_order() -> None:
    rows = observation.pose_rows(
        pickup_pose={"x": 0.0, "y": 0.0, "z": 0.0},
        climb_samples=[{"x": 0.0, "y": 0.0, "z": -1.0}],
        route_pose={"x": 2.0, "y": 1.0, "z": -2.5},
        landing_samples=[
            {"x": 4.0, "y": 3.0, "z": -1.0},
            {"x": 5.0, "y": 5.0, "z": 0.0},
        ],
        completed_pose={"x": 5.0, "y": 5.0, "z": 0.0},
    )
    assert [row["phase"] for row in rows] == [
        "pickup",
        "climb",
        "route",
        "landing",
        "landing",
        "completed",
    ]
    assert rows[1]["sample_index"] == 0
    assert rows[4]["sample_index"] == 1

    summary = observation.terminal_pose_summary_fields(
        route_pose={"x_m": "2.5", "y_m": 1, "z_m": -2.5},
        route_terminal_local_ned_pose={
            "local_x_m": 2.4,
            "local_y_m": 1.1,
            "local_z_m": -2.6,
        },
        completed_pose=None,
        landing_samples=[{"x": 5.0, "y": 5.0, "z": 0.0}],
        route_terminal_progress_m=7.25,
    )
    assert summary["route_terminal_pose"]["x_m"] == 2.5
    assert summary["route_terminal_local_ned_pose"]["source"] == (
        "px4_local_position_ned"
    )
    assert summary["landing_terminal_pose"]["sample_index"] == 0
    assert summary["completed_terminal_pose"]["observed"] is False
    assert summary["completed_terminal_pose"]["source"] == ""


def test_contact_topic_selection_and_evidence_are_read_only() -> None:
    alternate = "/world/default/model/mission_designer_collision_obstacle/contact"
    topic_list = f"/clock\n{alternate}\n{CONTACT_TOPIC}\n"
    assert observation.select_contact_topic(
        topic_list,
        configured_topic=CONTACT_TOPIC,
    ) == CONTACT_TOPIC

    fallback = observation.select_contact_topic(
        f"/clock\n{alternate}\n",
        configured_topic=CONTACT_TOPIC,
    )
    assert fallback == alternate

    evidence = observation.contact_topic_observation(
        topic_list=topic_list,
        sample_text="collision { id: 1 }",
        sample_returncode=0,
        configured_topic=CONTACT_TOPIC,
    )
    assert evidence["topic"] == CONTACT_TOPIC
    assert evidence["topic_advertised"] is True
    assert evidence["contact_event_observed"] is True
    assert evidence["read_only_observer"] is True
    assert evidence["task_status_mutated"] is False
    assert evidence["delivery_completion_claimed"] is False


def test_distance_and_xy_helpers_share_one_geometry_contract() -> None:
    assert observation.distance_to_segment_xy(
        point_xy=(1.0, 1.0),
        start_xy=(0.0, 0.0),
        end_xy=(2.0, 0.0),
    ) == 1.0
    assert observation.point_to_segment_distance_m(
        (1.0, 1.0),
        (0.0, 0.0),
        (2.0, 0.0),
    ) == 1.0
    assert observation.distance_to_segment_xy(
        point_xy=(1.0, 1.0),
        start_xy=(0.0, 0.0),
        end_xy=(0.0, 0.0),
    ) == math.sqrt(2.0)
    assert observation.xy_pairs_match([1.0, 2.0], [1.0 + 1e-7, 2.0]) is True
    assert observation.xy_pairs_match([1.0, 2.0], [1.01, 2.0]) is False
    assert observation.xy_pairs_match(None, [1.0, 2.0]) is False
