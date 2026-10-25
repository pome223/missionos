"""Typed desired outcomes, separate from measurements and execution authority.

The generic count predicate is projected by a deployment adapter. Its private
proof binds registered Ops and an actual sanitized checkpoint, not a predicted
or prematurely closed origin. The independent saved checker reconstructs these
facts from its own trusted inputs; this producer module grants no permission.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import json
import math
import re
from types import MappingProxyType

INTENT_SCHEMA = "missionos.operational_intent.v1"
OBSERVATION_SCHEMA = "missionos.operational_intent_observation.v1"
REQUEST_SCHEMA_V2 = "missionos.starship_restored_decision_request.v2"
OPS_SCHEMA_V2 = "missionos.starship_restored_native_operations.v2"
ACTIONS = ("continue_deployment", "withhold_deployment")
METRICS = ("inventory.released_count", "inventory.retained_count")
COMPARISONS = ("eq", "gte", "lte")
NUMERIC_KEYS = frozenset({"intent.active", "intent.generation", "intent.target_value", "intent.observed_value",
    *(f"effect.{action}.{key}" for action in ACTIONS for key in (
        "released_count_if_completed", "retained_count_if_completed", "intent_met_if_completed"))})


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def _wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _copy(value):
    return json.loads(_wire(value))


def _hash(value):
    return type(value) is str and re.fullmatch("[a-f0-9]{64}", value) is not None


def _count(value):
    return type(value) is int and 0 <= value <= 1000000


def validate_intent(value):
    _require(type(value) is dict and set(value) == {"schema", "intent_id", "generation", "issuer",
        "intent_class", "evaluation_point", "trigger", "objective"}
        and value["schema"] == INTENT_SCHEMA and type(value["intent_id"]) is str
        and re.fullmatch("[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}", value["intent_id"]) is not None
        and _count(value["generation"]) and value["issuer"] == "registered_operations"
        and value["intent_class"] == "desired_outcome_not_authority"
        and value["evaluation_point"] == "closed_inventory", "restored_intent_definition_invalid")
    trigger, goal = value["trigger"], value["objective"]
    _require(type(trigger) is dict and set(trigger) == {"phase", "released_count"}
        and trigger["phase"] == "deployment" and _count(trigger["released_count"]), "restored_intent_trigger_invalid")
    _require(type(goal) is dict and set(goal) == {"metric", "comparison", "value", "unit"}
        and goal["metric"] in METRICS and goal["comparison"] in COMPARISONS
        and _count(goal["value"]) and goal["unit"] == "count", "restored_intent_objective_invalid")
    return _copy(value)


def evaluate_objective(intent, *, released_count, retained_count):
    goal = validate_intent(intent)["objective"]
    _require(_count(released_count) and _count(retained_count), "restored_intent_actual_counts_invalid")
    actual = released_count if goal["metric"] == "inventory.released_count" else retained_count
    return {"eq": actual == goal["value"], "gte": actual >= goal["value"], "lte": actual <= goal["value"]}[goal["comparison"]]


def _validate_checkpoint(value):
    _require(type(value) is dict and set(value) == {"phase", "time_s", "released_count", "retained_count",
        "total_count", "release_ready", "sequencer_state"} and value["phase"] == "deployment"
        and type(value["time_s"]) in (int, float) and math.isfinite(value["time_s"]) and value["time_s"] >= 0
        and all(_count(value[key]) for key in ("released_count", "retained_count", "total_count"))
        and value["total_count"] > 0 and value["released_count"]+value["retained_count"] == value["total_count"]
        and type(value["release_ready"]) is bool and type(value["sequencer_state"]) is str
        and re.fullmatch("[A-Za-z0-9_-]{1,64}", value["sequencer_state"]) is not None,
        "restored_intent_checkpoint_invalid")
    return _copy(value)


def validate_observation(value):
    _require(type(value) is dict and set(value) == {"schema", "intent", "operations_sha256", "source_sha256",
        "scope_sha256", "checkpoint", "checkpoint_sha256", "authority_granted", "closed_inventory_inferred",
        "physical_release_inferred"} and value["schema"] == OBSERVATION_SCHEMA
        and all(_hash(value[key]) for key in ("operations_sha256", "source_sha256", "scope_sha256", "checkpoint_sha256"))
        and all(value[key] is False for key in ("authority_granted", "closed_inventory_inferred", "physical_release_inferred")),
        "restored_intent_observation_invalid")
    intent = validate_intent(value["intent"])
    actual = _validate_checkpoint(value["checkpoint"])
    _require(intent["trigger"]["released_count"] == actual["released_count"]
        and intent["objective"]["value"] <= actual["total_count"]
        and sha256(_wire(actual)).hexdigest() == value["checkpoint_sha256"], "restored_intent_checkpoint_binding_changed")
    return _copy(value)


def project_effects(observation):
    value = validate_observation(observation)
    actual, intent = value["checkpoint"], value["intent"]
    result = {}
    for action in ACTIONS:
        released = actual["total_count"] if action == "continue_deployment" else actual["released_count"]
        retained = actual["total_count"]-released
        result[action] = {"released_count_if_completed": released, "retained_count_if_completed": retained,
            "intent_met_if_completed": evaluate_objective(intent, released_count=released, retained_count=retained),
            "conditional_on_all_remaining_releases_passing_independent_gates": action == "continue_deployment",
            "physical_release_or_success_inferred": False, "authority_granted": False}
    return result


def numeric_projection(observation):
    value = validate_observation(observation)
    intent, actual = value["intent"], value["checkpoint"]
    result = {"intent.active": True, "intent.generation": intent["generation"],
        "intent.target_value": intent["objective"]["value"],
        "intent.observed_value": actual["released_count"] if intent["objective"]["metric"] == METRICS[0] else actual["retained_count"]}
    for action, effects in project_effects(value).items():
        result.update({f"effect.{action}.{key}": effects[key] for key in (
            "released_count_if_completed", "retained_count_if_completed", "intent_met_if_completed")})
    return result


def public_goal(observation):
    value = validate_observation(observation)
    intent = value["intent"]
    return {key: intent[key] for key in ("intent_id", "generation", "issuer", "intent_class", "evaluation_point", "objective")} | {
        "authority_granted": False, "arbitrary_notice_prose_is_not_a_registered_goal": True}


@dataclass(frozen=True, init=False)
class OperationalIntentNotice:
    """Registered Ops plus the actual callback checkpoint; no closed origin."""

    _json: bytes = field(repr=False)
    _notice_summary: str = field(repr=False)

    def __init__(self, intent, *, operations_sha256, summary, notice_summary):
        typed = validate_intent(intent)
        _require(_hash(operations_sha256) and type(summary) in (dict, MappingProxyType)
            and type(notice_summary) is str and len(notice_summary) <= 600,
            "restored_intent_requires_registered_actual_checkpoint")
        actual = _validate_checkpoint({"phase": "deployment", "time_s": summary["time_s"],
            "released_count": summary["released_count"], "retained_count": summary["retained_count"],
            "total_count": summary["released_count"]+summary["retained_count"],
            "release_ready": summary["release_ready"], "sequencer_state": summary["sequencer_state"]})
        _require(actual["released_count"] == typed["trigger"]["released_count"]
            and typed["objective"]["value"] <= actual["total_count"], "restored_intent_trigger_not_actual")
        object.__setattr__(self, "_json", _wire({"intent": typed, "operations_sha256": operations_sha256,
            "checkpoint": actual, "checkpoint_sha256": sha256(_wire(actual)).hexdigest()}))
        object.__setattr__(self, "_notice_summary", notice_summary)

    @property
    def notice_summary(self):
        return self._notice_summary

    def observe(self, *, source_sha256, scope_sha256):
        value = json.loads(self._json)
        return validate_observation({"schema": OBSERVATION_SCHEMA, **value,
            "source_sha256": source_sha256, "scope_sha256": scope_sha256, "authority_granted": False,
            "closed_inventory_inferred": False, "physical_release_inferred": False})
