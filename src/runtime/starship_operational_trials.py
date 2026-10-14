"""Seeded synthetic recovery operations; latent schedules stay outside policy input.

These trials vary reported recovery conditions and message delivery, not vehicle
physics. Historical precedents justify kinds of events, never their frequency,
severity or a Starship-specific operating threshold. The fixed stratified design
contains one of each profile per split and estimates no real incident rate.
The separately registered scope-closure case is targeted coverage, not another
sample from that original design or an unseen-condition evaluation.
"""

from copy import deepcopy
from functools import lru_cache
from hashlib import sha256
import json
import math
from types import MappingProxyType

GENERATOR = "sha256_named_draw_v1"
SCHEMA = "missionos.starship_operational_trial.v1"
FORECAST_BASIS = "synthetic_uncertain_estimate_not_authority_or_observed_completion"
SOURCE_PRECEDENTS = MappingProxyType({
    "weather": "nasa_crew8_weather_20241016",
    "delivery": "nasa_orion_comms_20221123",
})
PROFILES = (
    "nominal",
    "clearance_interruption",
    "service_forecast_correction",
    "delivery_outage",
)
# Opaque public identifiers; split, profile and seed are harness metadata only.
TRIALS = MappingProxyType({
    f"op_trial_{index + 1:02d}": MappingProxyType({
        "seed": seed,
        "split": "development" if index < 4 else "evaluation",
        "profile": PROFILES[index % 4],
    })
    for index, seed in enumerate((730101, 730219, 730337, 730453, 910103, 910229, 910349, 910463))
})

SCOPE_CLOSURE_CASE = "op_scope_01"
REGISTERED_CASES = (*TRIALS, SCOPE_CLOSURE_CASE)


def is_trial(case):
    return type(case) is str and case in REGISTERED_CASES


class _Draws:
    def __init__(self, seed):
        self.seed = seed
        self.draws = {}

    def uniform(self, name, lower, upper):
        # A labelled SHA-256 draw is independent of Python/random versions and
        # process-global PRNG state. Every named draw is made exactly once.
        if name in self.draws:
            raise ValueError("duplicate_operational_trial_draw")
        encoded = f"{GENERATOR}\0{self.seed}\0{name}".encode("ascii")
        integer = int.from_bytes(sha256(encoded).digest()[:8], "big")
        fraction = integer / 2**64
        value = round(lower + fraction * (upper - lower), 3)
        self.draws[name] = {"range": [lower, upper], "value": value}
        return value


def _weather(draws):
    return {
        "basis": "synthetic_reported_measurement_not_vehicle_weather_forcing",
        "measured_at_s": 890.0,
        "east": {
            "wind_speed_mps": draws.uniform("east_wind_mps", 4.0, 9.0),
            "significant_wave_height_m": draws.uniform("east_waves_m", 0.5, 1.4),
        },
        "west": {
            "wind_speed_mps": draws.uniform("west_wind_mps", 4.0, 9.0),
            "significant_wave_height_m": draws.uniform("west_waves_m", 0.5, 1.4),
        },
    }


def _notice(sequence, issued, forecast, weather, *, expires=12000.0, areas=None, text=None):
    return {
        "sequence": sequence,
        "issued_at_s": issued,
        "expires_at_s": expires,
        "areas": deepcopy(
            {"east": [4500.0, 5600.0], "west": [10000.0, 11000.0]}
            if areas is None else areas
        ),
        "text": text or (
            "Recovery coordinator: both study areas retain explicit operating clearance for "
            "the stated windows. Service-delay intervals are uncertain estimates, not observed "
            "completion or authority to use an area. Reported weather is informational; this "
            "publication does not change clearance or service estimates."
        ),
        "recovery_service_forecast": deepcopy(forecast),
        "reported_weather": deepcopy(weather),
    }


def _build_manifest(case):
    """Return the frozen reproducible harness manifest, never a model input.

    All draws and future publications are deliberately visible to the experiment
    harness for preregistration/audit. Calling this function does not run a model,
    a prediction or a flight, and cannot alter any other trial's random sequence.
    """
    if not is_trial(case):
        raise ValueError("unknown_starship_operational_trial")
    if case == SCOPE_CLOSURE_CASE:
        return _scope_closure_manifest()
    spec = TRIALS[case]
    draws = _Draws(spec["seed"])
    weather = _weather(draws)
    east_lower = draws.uniform("initial_east_delay_lower_s", 450.0, 750.0)
    west_lower = draws.uniform("initial_west_delay_lower_s", 850.0, 1250.0)
    forecast = {
        "basis": FORECAST_BASIS,
        "east_delay_after_contact_s": [
            east_lower, round(east_lower + draws.uniform("initial_east_delay_width_s", 400.0, 750.0), 3)
        ],
        "west_delay_after_contact_s": [
            west_lower, round(west_lower + draws.uniform("initial_west_delay_width_s", 600.0, 1100.0), 3)
        ],
    }
    publications = []

    def publish(notice, delay):
        publications.append({
            "delivery_at_s": round(notice["issued_at_s"] + delay, 3),
            "notice": notice,
        })

    initial_expiry = (
        draws.uniform("initial_expiry_s", 1180.0, 1240.0)
        if spec["profile"] == "delivery_outage" else 12000.0
    )
    publish(
        _notice(1, 900.0, forecast, weather, expires=initial_expiry),
        draws.uniform("initial_delivery_delay_s", 5.0, 35.0),
    )
    if spec["profile"] == "nominal":
        # Only sequence and publication time change. A routine publication must
        # not fabricate a material change in clearance, measurements or costs.
        issued = draws.uniform("routine_issue_s", 1250.0, 1530.0)
        publish(_notice(2, issued, forecast, weather), draws.uniform("routine_delay_s", 1.0, 30.0))
    elif spec["profile"] == "clearance_interruption":
        issued = draws.uniform("withdrawal_issue_s", 1200.0, 1370.0)
        interrupted_weather = deepcopy(weather)
        interrupted_weather["measured_at_s"] = round(issued - 10.0, 3)
        interrupted_weather["east"] = {
            "wind_speed_mps": draws.uniform("interruption_wind_mps", 13.0, 20.0),
            "significant_wave_height_m": draws.uniform("interruption_waves_m", 2.0, 3.5),
        }
        reopening_estimate = [round(issued + 180.0, 3), round(issued + 650.0, 3)]
        interrupted = _notice(
            2, issued, forecast, interrupted_weather,
            areas={"west": [10000.0, 11000.0]},
            text=(
                "Recovery coordinator: eastern-area clearance is withdrawn during adverse "
                "reported weather and support-vessel repositioning. Western clearance remains "
                "as published. A reopening estimate is uncertain and does not reinstate clearance; "
                "only a later delivered clearance publication can do that."
            ),
        )
        interrupted["clearance_reopening_estimate"] = {
            "basis": "synthetic_uncertain_estimate_not_clearance_or_future_truth",
            "east_time_s": reopening_estimate,
        }
        publish(interrupted, draws.uniform("withdrawal_delay_s", 2.0, 35.0))
        reinstated_at = draws.uniform("reinstatement_issue_s", 1670.0, 1940.0)
        resumed_weather = deepcopy(weather)
        resumed_weather["measured_at_s"] = round(reinstated_at - 10.0, 3)
        publish(_notice(
            3, reinstated_at, forecast, resumed_weather,
            text=(
                "Recovery coordinator: eastern clearance is reinstated for its stated window "
                "following a fresh assessment. Western clearance is unchanged. Check the "
                "current predicted arrival, available resources and both published windows."
            ),
        ), draws.uniform("reinstatement_delay_s", 2.0, 45.0))
    elif spec["profile"] == "service_forecast_correction":
        issued = draws.uniform("forecast_update_issue_s", 1250.0, 1440.0)
        revised = deepcopy(forecast)
        lower = draws.uniform("east_delayed_lower_s", 6000.0, 7800.0)
        revised["east_delay_after_contact_s"] = [
            lower, round(lower + draws.uniform("east_delayed_width_s", 2300.0, 4500.0), 3)
        ]
        publish(_notice(
            2, issued, revised, weather,
            text=(
                "Recovery coordinator correction: both area clearances remain in force. "
                "The uncertain eastern post-contact service-delay estimate has increased; "
                "western service estimates are unchanged. These are estimates of support "
                "availability, not observations of completed recovery or new clearance."
            ),
        ), draws.uniform("forecast_update_delay_s", 3.0, 40.0))
        corrected_at = draws.uniform("forecast_correction_issue_s", 1680.0, 1910.0)
        corrected = deepcopy(revised)
        lower = draws.uniform("corrected_east_lower_s", 3600.0, 6600.0)
        corrected["east_delay_after_contact_s"] = [
            lower, round(lower + draws.uniform("corrected_east_width_s", 2800.0, 5200.0), 3)
        ]
        publish(_notice(
            3, corrected_at, corrected, weather,
            text=(
                "Recovery coordinator revision: replace the preceding eastern service-delay "
                "estimate with this interval after updated support-team information. It remains "
                "uncertain. Both operating clearances and western estimates remain unchanged. "
                "Compare the currently available costs and completion estimates; no recovery "
                "has yet been observed."
            ),
        ), draws.uniform("forecast_correction_delay_s", 3.0, 45.0))
    else:
        # Only operations-message delivery is delayed. This is not a simulated
        # spacecraft telemetry outage or a declaration of missing vehicle data.
        issued = draws.uniform("delayed_update_issue_s", 1250.0, 1340.0)
        delivered = draws.uniform("outage_delivery_restored_s", 1530.0, 1780.0)
        delayed = _notice(
            2, issued, forecast, weather,
            text=(
                "Recovery coordinator: this bulletin arrived after an operations-message "
                "delivery delay. Use its issuance and expiry times when assessing freshness. "
                "Both area clearances and service-delay estimates are as stated. This notice "
                "says nothing about spacecraft telemetry-link health."
            ),
        )
        publish(delayed, delivered - issued)
    publications.sort(key=lambda row: row["delivery_at_s"])
    return {
        "schema": SCHEMA,
        "case": case,
        "generator": GENERATOR,
        **dict(spec),
        "sampling": "fixed_stratified_design_not_estimated_real_incident_probabilities",
        "parameter_basis": "synthetic_engineering_assumptions_not_historical_rates_or_thresholds",
        "source_precedent_ids": [SOURCE_PRECEDENTS[
            "delivery" if spec["profile"] == "delivery_outage" else "weather"
        ]],
        "precedent_scope": (
            "communications_loss_is_analogy_for_operations_message_delivery_only"
            if spec["profile"] == "delivery_outage"
            else "weather_and_recovery_operations_updates_not_exact_incident_reenactment"
        ),
        "model_calls": 0,
        "physics_fault_injection": False,
        "draws": draws.draws,
        "publications": publications,
    }


def _scope_closure_manifest():
    """Targeted coverage: retain the original two delivered notices only.

    No model input contains the future schedule or an expected response. The
    withdrawn clearance remains unavailable until a new publication actually
    arrives; this bounded scenario publishes no reinstatement.
    """
    source = _build_manifest("op_trial_02")
    publications = deepcopy(source["publications"][:2])
    withdrawal = publications[1]["notice"]
    withdrawal.pop("clearance_reopening_estimate")
    withdrawal["text"] = (
        "Recovery coordinator: eastern-area clearance is withdrawn during adverse "
        "reported weather and support-vessel repositioning. Western clearance remains "
        "as published. No revised eastern clearance is currently issued. Service-delay "
        "estimates are not operating clearance."
    )
    return {
        "schema": SCHEMA, "case": SCOPE_CLOSURE_CASE,
        "generator": "fixed_scope_closure_schedule_v1",
        "split": "targeted_scope_coverage", "profile": "clearance_withdrawal",
        "sampling": "targeted_scope_coverage_not_held_out_or_incident_rate_estimate",
        "parameter_basis": "synthetic_engineering_assumptions_not_historical_rates_or_thresholds",
        "source_precedent_ids": [SOURCE_PRECEDENTS["weather"]],
        "precedent_scope": "weather_and_recovery_operations_updates_not_exact_incident_reenactment",
        "schedule_basis": "first_two_op_trial_02_publications_without_reinstatement",
        "model_calls": 0, "physics_fault_injection": False, "draws": {},
        "publications": publications,
    }


@lru_cache(maxsize=len(REGISTERED_CASES))
def _manifest_json(case):
    # Cache immutable bytes, never a mutable manifest shared with callers.
    return json.dumps(_build_manifest(case), sort_keys=True, separators=(",", ":"), allow_nan=False)


def trial_manifest(case):
    """Fresh JSON-native copy of the preregistered harness-only schedule."""
    if not is_trial(case):
        raise ValueError("unknown_starship_operational_trial")
    return json.loads(_manifest_json(case))


@lru_cache(maxsize=len(REGISTERED_CASES))
def _publications(case):
    return tuple(
        (row["delivery_at_s"], row["notice"]["sequence"], json.dumps(row["notice"], allow_nan=False))
        for row in trial_manifest(case)["publications"]
    )


def delivered_notice(case, time_s):
    """Read only the latest delivered publication, including an expired one.

    No future publication, recovery truth, scenario label or PRNG metadata is
    returned. A delayed unpublished message cannot change the observed notice.
    The caller evaluates freshness from the unchanged issue/expiry timestamps.
    """
    if type(time_s) not in (int, float) or not math.isfinite(time_s) or time_s < 0:
        raise ValueError("invalid_operational_trial_time")
    if not is_trial(case):
        raise ValueError("unknown_starship_operational_trial")
    available = [row for row in _publications(case) if row[0] <= time_s]
    if not available:
        return None
    # A late old bulletin cannot roll back a newer publication if a future
    # generator version introduces out-of-order transport.
    return json.loads(max(available, key=lambda row: row[1])[2])
