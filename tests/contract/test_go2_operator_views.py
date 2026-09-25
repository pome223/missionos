from missionos_cli.go2_operator_views import (
    html_map,
    is_go2_task,
    local_map_model,
    summary_lines,
    terminal_map,
)


def task(status="completed"):
    return {
        "task_id": "go2_0123456789abcdef",
        "kind": "go2_delivery_execution",
        "status": status,
        "artifacts": {
            "go2_delivery_proposal": {
                "scenario": "moving_obstacle",
                "plan": {"home_xy": [-2.5, 0], "destination_xy": [2.5, 0]},
            },
            "go2_delivery_approval": {"operator_approval_ref": "test"},
            "go2_delivery_snapshot": {
                "phase": "Completed",
                "xy": [-2.48, 0.02],
                "sim_time_s": 84.4,
                "moving_obstacle": {"xy": [-1.25, -2.6]},
            },
            "go2_trajectory": [[-2.5, 0], [-1.5, 1], [2.4, 0], [-2.48, 0.02]],
            "go2_local_avoidance": {"event": "dynamic_path_clear_observed"},
            "go2_delivery_result": {
                "completion_claimed": True,
                "receipt": {"source": "simulation_recipient", "received": True},
                "events": [{"event": "terminal_hold_verified"}],
                "dynamic_avoidance": {"yield_count": 2, "contact_physics_steps": 0},
            },
        },
    }


def test_go2_operator_views_use_one_task_and_observed_indoor_coordinates():
    item = task()
    assert is_go2_task(item)
    lines = "\n".join(summary_lines(item))
    assert "go2_0123456789abcdef" in lines
    assert "simulated delivery and return verified: True" in lines
    assert "physical_execution=False" in lines
    assert "SITL:" not in lines
    model = local_map_model(item)
    assert model["trail"][-1] == (-2.48, 0.02)
    assert model["moving_obstacle"] == (-1.25, -2.6)
    assert "R" in terminal_map(model) and "D" in terminal_map(model)
    page = html_map(model)
    assert "Go2 屋内配送マップ" in page
    assert "missionos_go2_indoor_map.v1" in page
    assert "openstreetmap" not in page.lower()


def test_proposed_go2_task_does_not_claim_receipt_or_completion():
    item = task("proposed")
    item["artifacts"].pop("go2_delivery_result")
    item["artifacts"].pop("go2_delivery_approval")
    item["artifacts"]["go2_delivery_snapshot"]["xy"] = [float("nan"), 0]
    lines = "\n".join(summary_lines(item))
    assert "operator_approved=False" in lines
    assert "Position: unobserved" in lines
    assert "simulated delivery and return verified: False" in lines
    assert local_map_model(item)["current"] is None


def test_completed_label_requires_receipt_and_terminal_hold_evidence():
    item = task()
    item["artifacts"]["go2_delivery_result"].pop("events")
    assert "simulated delivery and return verified: False" in "\n".join(summary_lines(item))
    item["artifacts"]["go2_delivery_result"]["events"] = [{"event": "terminal_hold_verified"}]
    item["artifacts"]["go2_delivery_result"]["receipt"]["received"] = False
    assert "Receipt observed: False" in "\n".join(summary_lines(item))
    assert local_map_model(item)["delivery_and_return_verified"] is False
