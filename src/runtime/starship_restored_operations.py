"""Immutable registered synthetic operations, observed at the actual coast clock.

The definition contains ground notices and accounting assumptions only. Binding
happens once, after the actual prefix closes its inventory. This tool supplies
no forecasts, state assignments, approval, dispatch or fault truth. Its snapshot
caller and the separate saved checker establish actual-state provenance.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import json
from types import MappingProxyType

from . import starship_restored_admission as admission
from . import starship_restored_contract as contract
from . import starship_sixdof as dyn
from .starship_restored_intent import OPS_SCHEMA_V2, OperationalIntentNotice, validate_intent

SCHEMA = "missionos.starship_restored_native_operations.v1"
BOUND_SCHEMA = "missionos.starship_restored_bound_operations.v1"


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _same(first, second):
    return _wire(first) == _wire(second)


def _site(row):
    _require(type(row) is dict, "restored_operations_site_record_required")
    typed = admission.RestoredSitePolicy(**{key: row[key] for key in (
        "site_id", "latitude_deg", "longitude_deg", "radius_m", "opens_s", "closes_s")})
    _require(_same(row, typed.record()), "restored_operations_site_shape_changed")
    return typed


def _notice(row):
    _require(type(row) is dict, "restored_operations_notice_record_required")
    typed = admission.RestoredOperationalNotice(row["generation"], row["issued_s"], row["expires_s"],
        tuple(row["available_site_ids"]))
    _require(_same(row, typed.record()), "restored_operations_notice_shape_changed")
    return typed


@dataclass(frozen=True, init=False)
class RegisteredRestoredOperations:
    """Frozen source-visible definition; safe to create before any plant starts."""

    _json: bytes = field(repr=False)

    def __init__(self, operations):
        intent_mode = type(operations) is dict and operations.get("schema") == OPS_SCHEMA_V2
        keys = {"schema", "sites", "notice_schedule", "deployment_notices", "resource_accounting"}
        _require(type(operations) is dict and set(operations) == keys | ({"deployment_intents"} if intent_mode else set())
            and operations["schema"] in (SCHEMA, OPS_SCHEMA_V2),
            "restored_operations_registered_definition_required")
        sites, schedule = operations["sites"], operations["notice_schedule"]
        deployment = operations["deployment_notices"]
        _require(type(sites) is list and 1 <= len(sites) <= 16 and type(schedule) is list and schedule
            and type(deployment) is dict and set(deployment) == {"0", "13"}
            and all(type(text) is str and len(text) <= 600 for text in deployment.values()),
            "restored_operations_definition_shape")
        identifiers = [value.site_id for value in map(_site, sites)]
        _require(len(identifiers) == len(set(identifiers)), "restored_operations_duplicate_site")
        generations, issue_times = [], []
        for item in schedule:
            _require(type(item) is dict and set(item) == {"notice", "summary"}
                and type(item["summary"]) is str and len(item["summary"]) <= 600,
                "restored_operations_notice_summary_shape")
            notice = _notice(item["notice"])
            _require(set(notice.available_site_ids) <= set(identifiers), "restored_operations_unknown_site")
            generations.append(notice.generation)
            issue_times.append(notice.issued_s)
        _require(generations == sorted(set(generations)) and issue_times == sorted(set(issue_times)),
            "restored_operations_notice_order_or_generation_changed")
        waiting = admission.RestoredAdmissionPolicy().record()["waiting"]
        resources = {"epoch": "closed_origin", "initial_energy_kwh": waiting["initial_energy_kwh"],
            "power_kw": waiting["power_kw"], "initial_temperature_k": waiting["initial_temperature_k"],
            "warming_k_per_s": waiting["warming_k_per_s"], "initial_modeled_propellant_loss_kg": 0.,
            "modeled_loss_kg_per_s": waiting["modeled_loss_kg_per_s"]}
        _require(_same(operations["resource_accounting"], resources),
            "restored_operations_fixed_accounting_assumptions_changed")
        if intent_mode:
            intents = operations["deployment_intents"]
            _require(type(intents) is dict and set(intents) == {"0", "13"}, "restored_operations_intent_slots_changed")
            identities = []
            for slot, value in intents.items():
                if value is not None:
                    goal = validate_intent(value)
                    _require(goal["trigger"]["released_count"] == int(slot) and goal["objective"]["value"] <= 26,
                        "restored_operations_intent_trigger_or_total_changed")
                    identities.append((goal["intent_id"], goal["generation"]))
            _require(len(identities) == len(set(identities)), "restored_operations_duplicate_intent_generation")
        object.__setattr__(self, "_json", _wire(operations))

    def record(self):
        return json.loads(self._json)

    @property
    def sites(self):
        return tuple(_site(row) for row in self.record()["sites"])

    @property
    def notice_generations(self):
        return tuple(row["notice"]["generation"] for row in self.record()["notice_schedule"])

    def deployment_notice(self, summary):
        _require(type(summary) in (dict, MappingProxyType) and type(summary.get("released_count")) is int
            and summary["released_count"] in (0, 13)
            and type(summary.get("retained_count")) is int
            and summary["retained_count"] == 26-summary["released_count"],
            "restored_operations_deployment_inventory_slot_changed")
        definition = self.record()
        text = definition["deployment_notices"][str(summary["released_count"])]
        if definition["schema"] == OPS_SCHEMA_V2:
            goal = definition["deployment_intents"][str(summary["released_count"])]
            if goal is not None:
                return OperationalIntentNotice(goal, operations_sha256=sha256(self._json).hexdigest(),
                    summary=summary, notice_summary=text)
        return text

    def bind(self, actual_closed_origin):
        return RestoredOperations(self, actual_closed_origin)


@dataclass(frozen=True, init=False)
class RestoredOperations:
    """Pure tool bound to the one actual prefix inventory-closure epoch.

    Queries do not reset an epoch or accumulate state. A stale/forged snapshot
    cannot authorize anything: the coordinator and independent saved checker
    still validate its source, monotone actual history, admission and authority.
    """

    _registration: RegisteredRestoredOperations = field(repr=False)
    _origin: contract.ActualRestoredOrigin = field(repr=False)

    def __init__(self, registration, actual_closed_origin):
        _require(type(registration) is RegisteredRestoredOperations
            and type(actual_closed_origin) is contract.ActualRestoredOrigin,
            "restored_operations_exact_registration_and_actual_origin_required")
        object.__setattr__(self, "_registration", registration)
        object.__setattr__(self, "_origin", actual_closed_origin)

    def __call__(self, snapshot):
        original = self._origin.record()
        _require(type(snapshot) is dict and snapshot.get("phase") == "orbital_coast"
            and snapshot.get("return_controller") is None and snapshot.get("completed") is False
            and _same(snapshot.get("vehicle"), original["vehicle"]),
            "restored_operations_actual_precontroller_coast_required")
        craft = dyn.vehicle_from_dict(original["vehicle"])
        outages = {row["engine_name"] for row in original["ascent_settings"]["ascent"]["shutdowns"]
            if row["engine_name"].startswith("ship_")}
        state = contract._state(snapshot["state"], craft, outages)
        _require(state.time_s >= self._origin.time_s
            and state.propellant_kg <= original["state"]["propellant_kg"]+1e-6,
            "restored_operations_snapshot_before_epoch_or_added_fuel")
        binding = snapshot.get("binding")
        _require(type(binding) is dict and binding.get("source_sha256") == sha256(_wire(original["source_sha256"])).hexdigest()
            and binding.get("profile_sha256") == original["profile_sha256"]
            and binding.get("inventory_receipt_chain_sha256") == original["inventory_receipt_chain_sha256"]
            and type(binding.get("inventory_generation")) is int and binding["inventory_generation"] == original["inventory_generation"]
            and type(binding.get("retained_count")) is int and binding["retained_count"] == original["retained_count"]
            and type(binding.get("released_count")) is int and binding["released_count"] == original["released_count"],
            "restored_operations_snapshot_source_or_inventory_changed")
        definition = self._registration.record()
        issued = [row for row in definition["notice_schedule"] if row["notice"]["issued_s"] <= state.time_s]
        _require(issued, "restored_operations_no_notice_issued_at_actual_time")
        # Preserve an expired newest notice so Rules reject it. Never select a
        # future generation or resurrect an earlier still-unexpired notice.
        notice = _notice(issued[-1]["notice"])
        policy = definition["resource_accounting"]
        elapsed = state.time_s-self._origin.time_s
        resources = admission.RestoredResourceState(notice.generation, state.time_s,
            policy["initial_energy_kwh"]-elapsed*policy["power_kw"]/3600.,
            policy["initial_temperature_k"]+elapsed*policy["warming_k_per_s"],
            policy["initial_modeled_propellant_loss_kg"]+elapsed*policy["modeled_loss_kg_per_s"])
        return notice, resources

    def notice_summary(self, notice):
        _require(type(notice) is admission.RestoredOperationalNotice, "restored_operations_exact_notice_required")
        matches = [row for row in self._registration.record()["notice_schedule"] if _same(row["notice"], notice.record())]
        _require(len(matches) == 1, "restored_operations_unregistered_notice_or_generation")
        return matches[0]["summary"]

    def record(self):
        original = self._origin.record()
        return {"schema": BOUND_SCHEMA, "operations_sha256": sha256(self._registration._json).hexdigest(),
            "closed_origin_sha256": sha256(_wire(original)).hexdigest(), "resource_epoch_s": self._origin.time_s,
            "synthetic_accounting_only": True, "time_epoch_rebased": False, "physical_state_modified": False,
            "prediction_used": False, "fault_truth_exposed": False, "mission_authority_granted": False,
            "dispatch_authorized": False, "qualification_refreshed": False}
