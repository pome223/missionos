"""Synthetic integration receipt extracted without historical runner imports.

Original helper: test_starship_restored_refinement.py at source2906a1fd.
This invented receipt is not a flight, model or qualification result.
"""
from src.runtime.starship_restored_integration import RestoredIntegrationPolicy


def integration_receipt(n):
    return {"schema": "missionos.starship_restored_integration_execution.v1",
        "policy": RestoredIntegrationPolicy(n).record(), "status": "completed", "failure_class": None,
        "trace": {"rk4_calls": 2, "rk4_substeps": 2*n, "commands_recomputed_inside_rk4": 0,
            "outer_control_cadence_changed": False, "fuel_event_search_changed": False, "contact_search_changed": False},
        "process_isolation_required": True, "internal_rk4_only": True, "physics_limits_changed": False,
        "control_policy_changed": False, "experimental": True, "qualification_refreshed": False,
        "mission_authority_granted": False, "execution_permission_granted": False,
        "hosted_model_calls": 0, "nested_sixdof_forecasts": 0}
