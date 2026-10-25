"""Shared coarse boostback algebra, never a six-DOF executor or authority.

The legacy catch wrapper retains its historical ideal-burn arithmetic. The
explicit water-return caller instead supplies measured finite-engine capacity
and keeps every bounded burn/coast estimate in its planning receipts.
"""
from dataclasses import asdict, dataclass
import math

WATER_POLICY_ID = "coupled_water_return_v1"
VERTICAL_TARGETS_MPS = (200., 400., 600., 800., 1000.)
MAXIMUM_BURN_ESTIMATE_STEPS = 1000


def length(vector):
    return math.sqrt(sum(x*x for x in vector))


@dataclass(frozen=True)
class WaterReturnSettings:
    maximum_plans: int = 2
    maximum_point_forecasts_per_plan: int = 15

    def __post_init__(self):
        if (type(self.maximum_plans) is not int or self.maximum_plans != 2
                or type(self.maximum_point_forecasts_per_plan) is not int
                or self.maximum_point_forecasts_per_plan != 15):
            raise ValueError("water_return_requires_fixed_planning_budget")

    def record(self):
        return asdict(self)


def coupled_boostback_plan(position, velocity, mass, fuel, *, exhaust_velocity_mps,
                           burn_force_n, coast_prediction, terminal_reserve,
                           development_candidate_index=None, burn_estimate=None):
    """Five fixed vertical commands, three coarse intercept corrections each.

    Callback predictions cannot assign vehicle state. ``burn_estimate=None``
    preserves the previous catch planner's exact algebra and receipt format.
    A finite burn callback adds its own explicitly approximate capacity receipt.
    """
    dry = mass-fuel
    candidates = []
    for target_vertical in VERTICAL_TARGETS_MPS:
        fall_time = (target_vertical+math.sqrt(target_vertical**2+19.6*position[2]))/9.8
        horizontal = [-position[i]/max(30., fall_time) for i in (0, 1)]
        remaining_mass, prediction, burn = mass, None, None
        for correction in range(3):
            difference = [horizontal[i]-velocity[i] for i in (0, 1)]+[target_vertical-velocity[2]]
            if burn_estimate is None:
                estimated_burn = length(difference)/max(1., burn_force_n/mass)
                difference[2] += 9.8*estimated_burn
                remaining_mass = mass*math.exp(-length(difference)/exhaust_velocity_mps)
            else:
                burn = burn_estimate(difference)
                estimated_burn = burn["elapsed_s"]
                remaining_mass = burn["remaining_mass_kg"]
            remaining_fuel = max(0., remaining_mass-dry)
            post_position = [position[i]+.5*(velocity[i]+horizontal[i])*estimated_burn for i in (0, 1)]
            post_position.append(position[2]+.5*(velocity[2]+target_vertical)*estimated_burn)
            prediction = coast_prediction(post_position, [*horizontal, target_vertical], remaining_mass, remaining_fuel)
            # The finite water candidate must publish the exact target whose
            # burn/coast forecast is attached. Preserve the legacy catch
            # arithmetic, including its historical last unforecast correction.
            if burn_estimate is None or correction < 2:
                for i in (0, 1):
                    horizontal[i] -= prediction["position_enu_m"][i]/max(30., prediction["elapsed_s"])
        speed = length(prediction["velocity_enu_mps"])
        after_stop = remaining_mass*math.exp(-(speed+60.)/exhaust_velocity_mps)-dry
        reserve = terminal_reserve(dry+max(0., after_stop))
        candidate = {"target_velocity_enu_mps": [*horizontal, target_vertical],
            "predicted_post_burn_fuel_kg": remaining_mass-dry,
            "predicted_point_model_after_arrest_fuel_kg": after_stop,
            "point_model_contact_propellant_reserve_kg": reserve,
            "point_model_contact_budget_met": bool(after_stop >= reserve), "prediction": prediction}
        if burn is not None:
            candidate["coarse_burn_estimate"] = burn
            candidate["point_model_contact_budget_met"] &= burn["requested_impulse_reached"]
        candidates.append(candidate)
    feasible = [c for c in candidates if c["point_model_contact_budget_met"]]
    chosen = max(feasible or candidates, key=lambda c: c["predicted_point_model_after_arrest_fuel_kg"]
                 -c["point_model_contact_propellant_reserve_kg"])
    if development_candidate_index is not None:
        chosen = candidates[development_candidate_index]
    return {"method": "finite_command_candidates_and_point_model_intercept", "candidates": candidates,
        "selected": chosen, "prediction_is_execution": False,
        "selection_rule": ("explicit_development_candidate" if development_candidate_index is not None
                           else "contact_budget_first_else_best_effort_surplus"),
        **({"development_candidate_index": development_candidate_index} if development_candidate_index is not None else {}),
        "contact_budget_candidate_count": len(feasible),
        "selected_contact_budget_met": chosen["point_model_contact_budget_met"],
        "limitations": "Local frame, slew-limited axis without attitude dynamics, no trim or finite burn integration in coarse planning; actual six-DOF outcome is independent of this estimate."}


def finite_burn_estimate(delta_velocity, mass, fuel, engine_capacity, *, gravity_mps2,
                         minimum_remaining_fuel_kg=0., maximum_elapsed_s=100.):
    """Bounded point estimate with measured availability/spool, never dynamics.

    A fixed full-throttle request gives only an optimistic available-impulse
    estimate. Engine lag/rate, changing mass, individual Isp and fuel exhaustion
    remain finite. Direction tracking, TVC and aero are not represented here.
    """
    dry, remaining = mass-fuel, fuel
    throttle = [row["throttle"] for row in engine_capacity]
    elapsed, impulse_velocity, steps = 0., 0., 0
    feasible = False
    while steps < MAXIMUM_BURN_ESTIMATE_STEPS:
        required = length([*delta_velocity[:2], delta_velocity[2]+gravity_mps2*elapsed])
        if impulse_velocity >= required:
            feasible = True
            break
        if (remaining <= minimum_remaining_fuel_kg+1e-6 or not engine_capacity
                or elapsed >= maximum_elapsed_s-1e-12):
            break
        dt = min(.1, maximum_elapsed_s-elapsed)
        after = [min(1., value+min(row["rate_per_s"], (1.-value)/row["tau_s"])*dt)
                 for row, value in zip(engine_capacity, throttle)]
        flow = sum(row["maximum_flow_kg_s"]*value for row, value in zip(engine_capacity, after))
        dt = min(dt, (remaining-minimum_remaining_fuel_kg)/max(flow, 1.))
        after = [min(1., value+min(row["rate_per_s"], (1.-value)/row["tau_s"])*dt)
                 for row, value in zip(engine_capacity, throttle)]
        flow = sum(row["maximum_flow_kg_s"]*value for row, value in zip(engine_capacity, after))
        force = sum(row["maximum_force_n"]*value for row, value in zip(engine_capacity, after))
        impulse_velocity += force/(dry+remaining)*dt
        remaining = max(minimum_remaining_fuel_kg, remaining-flow*dt)
        elapsed += dt
        throttle, steps = after, steps+1
    required = length([*delta_velocity[:2], delta_velocity[2]+gravity_mps2*elapsed])
    limited_fuel = remaining <= minimum_remaining_fuel_kg+1e-6
    limited_time = elapsed >= maximum_elapsed_s-1e-12
    feasible = bool(not (limited_fuel or limited_time) and (feasible or impulse_velocity >= required))
    termination = ("requested_impulse_reached" if feasible else "no_available_thrust" if not engine_capacity
                   else ("reserve_limited" if minimum_remaining_fuel_kg > 0 else "fuel_exhausted") if limited_fuel
                   else "time_limited" if limited_time else "step_limit")
    return {"elapsed_s": elapsed, "remaining_mass_kg": dry+remaining,
        "remaining_fuel_kg": remaining, "propellant_used_kg": fuel-remaining,
        "requested_impulse_reached": feasible, "termination": termination, "steps": steps,
        "minimum_remaining_fuel_kg": minimum_remaining_fuel_kg, "maximum_elapsed_s": maximum_elapsed_s,
        "available_impulse_velocity_mps": impulse_velocity, "requested_impulse_velocity_mps": required,
        "prediction_is_execution": False, "thrust_direction_and_tvc_modelled": False}


class WaterReturnPlanner:
    """One flight's immutable settings and finite, inspectable planning budget."""

    def __init__(self, settings, minimum_propellant_kg, *, boostback_fuel_floor_kg, maximum_boostback_burn_s):
        if type(settings) is not WaterReturnSettings:
            raise ValueError("typed_water_return_settings_required")
        if (type(minimum_propellant_kg) not in (int, float) or not math.isfinite(minimum_propellant_kg)
                or minimum_propellant_kg < 10000.):
            raise ValueError("water_return_requires_existing_surface_reserve")
        if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
               for value in (boostback_fuel_floor_kg, maximum_boostback_burn_s)):
            raise ValueError("water_return_requires_existing_boostback_limits")
        self.settings, self.minimum_propellant_kg, self.receipts = settings, minimum_propellant_kg, []
        self.boostback_fuel_floor_kg = boostback_fuel_floor_kg
        self.maximum_boostback_burn_s = maximum_boostback_burn_s

    def plan(self, position, velocity, observed, state, vehicle, profile, *, coast_prediction):
        if len(self.receipts) >= self.settings.maximum_plans:
            raise ValueError("water_return_planning_budget_exhausted")
        from . import starship_physics as env
        indices = [i for i, e in enumerate(vehicle.engines)
                   if not e.name.startswith("rcs_") and state.engine_states[i].available]
        capacity = [{"index": i, "name": vehicle.engines[i].name,
            "maximum_force_n": vehicle.engines[i].max_thrust_n,
            "maximum_flow_kg_s": vehicle.engines[i].max_thrust_n/(vehicle.engines[i].isp_s*env.STANDARD_GRAVITY_MPS2),
            "throttle": float(state.engine_states[i].throttle),
            "tau_s": vehicle.engines[i].throttle_time_constant_s,
            "rate_per_s": vehicle.engines[i].throttle_rate_per_s} for i in indices]
        force = sum(row["maximum_force_n"] for row in capacity)
        flow = sum(row["maximum_flow_kg_s"] for row in capacity)
        burns, predictions = [], []
        def burn(delta):
            if len(burns) >= self.settings.maximum_point_forecasts_per_plan:
                raise ValueError("water_return_burn_prediction_budget_exhausted")
            result = finite_burn_estimate(delta, observed["mass_kg"], state.propellant_kg, capacity,
                gravity_mps2=env.EARTH_MU_M3_S2/env.norm(state.r_eci_m)**2,
                minimum_remaining_fuel_kg=self.boostback_fuel_floor_kg,
                maximum_elapsed_s=self.maximum_boostback_burn_s)
            burns.append(result)
            return result
        def coast(*args):
            if len(predictions) >= self.settings.maximum_point_forecasts_per_plan:
                raise ValueError("water_return_coast_prediction_budget_exhausted")
            result = coast_prediction(*args)
            predictions.append(result)
            return result
        plan = coupled_boostback_plan(position, velocity, observed["mass_kg"], state.propellant_kg,
            exhaust_velocity_mps=force/max(flow, 1e-30) if force > 0 else profile["booster"]["engine_isp_s"]*env.STANDARD_GRAVITY_MPS2,
            burn_force_n=force, coast_prediction=coast,
            terminal_reserve=lambda _mass: self.minimum_propellant_kg, burn_estimate=burn)
        receipt = {"schema": "missionos.starship_water_return_plan.v1", "ordinal": len(self.receipts)+1,
            "time_s": float(state.time_s), "plan": plan, "engine_capacity": capacity,
            "available_main_thrust_n": force, "available_main_mass_flow_kg_s": flow,
            "minimum_terminal_propellant_kg": self.minimum_propellant_kg,
            "boostback_fuel_floor_kg": self.boostback_fuel_floor_kg,
            "maximum_boostback_burn_s": self.maximum_boostback_burn_s,
            "burn_estimates": burns, "point_coast_forecasts": predictions,
            "coarse_burn_estimate_count": len(burns), "point_coast_forecast_count": len(predictions),
            "coarse_burn_steps": sum(r["steps"] for r in burns),
            "nested_sixdof_forecasts": 0, "mission_authority_granted": False,
            "status": "coarse_candidate_only" if plan["selected_contact_budget_met"] else "no_feasible_return_plan",
            "prediction_is_execution": False}
        self.receipts.append(receipt)
        return receipt
