"""Human-approved policy contracts and persistent, bounded dispatch reservations.

This module grants no hardware authority. Callers supply trusted runtime facts;
model output and HTTP request bodies must never be used as those facts.
"""

from __future__ import annotations

# Keep the ROS Humble simulator runtime compatible with Python 3.10.
# ruff: noqa: UP017, FURB162
import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Range(Contract):
    minimum: float
    maximum: float

    @model_validator(mode="after")
    def valid_range(self):
        if not all(math.isfinite(v) for v in (self.minimum, self.maximum)):
            raise ValueError("non_finite_range")
        if self.minimum > self.maximum:
            raise ValueError("inverted_range")
        return self


class Exact(Contract):
    equals: bool | str


class ObjectRule(Contract):
    properties: dict[str, Range | Exact | ArrayRule | ObjectRule]


class ArrayRule(Contract):
    items: Range | Exact | ObjectRule
    min_items: int = Field(ge=1, le=10)
    max_items: int = Field(ge=1, le=10)

    @model_validator(mode="after")
    def valid_length(self):
        if self.min_items > self.max_items:
            raise ValueError("inverted_array_length")
        return self


ObjectRule.model_rebuild()


def parameter_matches(rule, value):
    if isinstance(rule, Range):
        return (
            type(value) in (int, float)
            and math.isfinite(value)
            and rule.minimum <= value <= rule.maximum
        )
    if isinstance(rule, Exact):
        return type(value) is type(rule.equals) and value == rule.equals
    if isinstance(rule, ArrayRule):
        return (
            isinstance(value, list)
            and rule.min_items <= len(value) <= rule.max_items
            and all(parameter_matches(rule.items, item) for item in value)
        )
    return (
        isinstance(value, dict)
        and set(value) == set(rule.properties)
        and all(parameter_matches(bound, value[key]) for key, bound in rule.properties.items())
    )


class Action(Contract):
    parameters: dict[str, Range | Exact | ArrayRule | ObjectRule] = Field(default_factory=dict)
    parameter_variants: list[dict[str, Range | Exact | ArrayRule | ObjectRule]] = Field(
        default_factory=list
    )
    max_uses: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def exclusive_parameter_schemas(self):
        if self.parameters and self.parameter_variants:
            raise ValueError("choose_parameters_or_variants")
        return self


class AssurancePolicy(Contract):
    version: Literal[1]
    policy_id: str = Field(min_length=1)
    mission_id: str = Field(min_length=1)
    mission_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_scope: Literal["fixture", "simulator"]
    mode: Literal["human", "shadow", "bounded"]
    expires_at: str
    max_observation_age_seconds: int = Field(ge=1, le=300)
    max_total_actions: int = Field(ge=1, le=100)
    preserve: list[str] = Field(min_length=1)
    actions: dict[str, Action] = Field(min_length=1)
    on_unresolved: Literal["request_human"]

    @model_validator(mode="after")
    def valid_policy(self):
        timestamp(self.expires_at)
        if type(self.version) is not int:
            raise ValueError("integer_version_required")
        if len(set(self.preserve)) != len(self.preserve) or any(
            not v.strip() for v in self.preserve
        ):
            raise ValueError("invalid_preserve_predicates")
        if any(not key.strip() for key in self.actions):
            raise ValueError("empty_action")
        return self

    @property
    def sha256(self) -> str:
        return digest(self.model_dump())


def timestamp(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timezone_required")
    return dt.astimezone(timezone.utc)


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError("duplicate_or_non_string_yaml_key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load_policy(path: Path) -> AssurancePolicy:
    data = path.read_text(encoding="utf-8")
    if len(data.encode()) > 65536:
        raise ValueError("policy_too_large")
    # Aliases permit recursive or unexpectedly shared contracts; v1 rejects them.
    try:
        if any(isinstance(token, (yaml.AliasToken, yaml.AnchorToken)) for token in yaml.scan(data)):
            raise ValueError("yaml_aliases_not_supported")
        return AssurancePolicy.model_validate(yaml.load(data, Loader=UniqueLoader))
    except yaml.YAMLError as exc:
        raise ValueError("invalid_policy_yaml") from exc


class PolicyStore:
    """Local operator trust boundary. Keep the DB outside agent-writable inputs.

    Reservations are committed before executor invocation, never refunded, and
    survive restarts, crashes, policy replacement and re-planning for a mission.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            self.path.chmod(0o600)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS policies (
                    sha TEXT PRIMARY KEY, body TEXT NOT NULL, approved_by TEXT NOT NULL,
                    approved_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS reservations (
                    mission TEXT NOT NULL, proposal TEXT NOT NULL, action TEXT NOT NULL,
                    policy_sha TEXT NOT NULL, candidate_sha TEXT NOT NULL,
                    PRIMARY KEY(mission, proposal));
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def approve(self, policy: AssurancePolicy, *, operator: str, expected_sha256: str):
        if not operator.strip() or expected_sha256 != policy.sha256:
            raise ValueError("explicit_operator_and_reviewed_hash_required")
        if timestamp(policy.expires_at) <= datetime.now(timezone.utc):
            raise ValueError("policy_expired")
        with self.connect() as db:
            # Re-approval must not resurrect revoked authority.
            db.execute(
                "INSERT OR IGNORE INTO policies(sha,body,approved_by,approved_at) VALUES(?,?,?,?)",
                (
                    policy.sha256,
                    policy.model_dump_json(),
                    operator,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return self.approved(policy.sha256)

    def approved(self, sha: str):
        with self.connect() as db:
            row = db.execute(
                "SELECT body,approved_by,approved_at,revoked FROM policies WHERE sha=?", (sha,)
            ).fetchone()
        if not row or row[3]:
            raise ValueError("approved_active_policy_required")
        policy = AssurancePolicy.model_validate_json(row[0])
        if policy.sha256 != sha:
            raise ValueError("stored_policy_hash_mismatch")
        return policy

    def revoke(self, sha: str):
        with self.connect() as db:
            if db.execute("UPDATE policies SET revoked=1 WHERE sha=?", (sha,)).rowcount != 1:
                raise ValueError("policy_not_found")

    def for_mission(self, mission_id: str):
        with self.connect() as db:
            rows = db.execute("SELECT sha,body FROM policies WHERE revoked=0").fetchall()
        matching = [sha for sha, body in rows if json.loads(body)["mission_id"] == mission_id]
        if len(matching) > 1:
            raise ValueError("multiple_active_mission_policies_revoke_superseded_versions")
        return self.approved(matching[0]) if matching else None

    def context(self, sha: str):
        policy = self.approved(sha)
        return {
            "policy_sha256": sha,
            "policy": policy.model_dump(),
            "authority_source": "human_approved_policy",
            "agent_may_modify_policy": False,
        }

    def check(
        self, sha: str, *, graph: dict, request: dict, facts: dict, consume: bool = False
    ) -> dict:
        from src.intelligence.missionos_mission_incident_continuation_graph import (
            _frozen_graph_reasons,
        )

        reasons = _frozen_graph_reasons(graph)
        try:
            policy = self.approved(sha)
        except ValueError as exc:
            return {"policy_authorized": False, "blocking_reasons": [str(exc)]}
        situation = graph.get("mission_situation", {})
        constraints = situation.get("constraints", {}).get("mission_context", {})
        expected_context = {
            "policy_sha256": sha,
            "policy": policy.model_dump(),
            "authority_source": "human_approved_policy",
            "agent_may_modify_policy": False,
        }
        if constraints.get("assurance_policy") != expected_context:
            reasons.append("policy_not_bound_to_assurance_judgment")
        if timestamp(policy.expires_at) <= datetime.now(timezone.utc):
            reasons.append("policy_expired")
        if (
            request.get("task_id") != policy.mission_id
            or facts.get("mission_id") != policy.mission_id
            or situation.get("progress", {}).get("task_id") != policy.mission_id
        ):
            reasons.append("policy_mission_mismatch")
        if (
            facts.get("execution_scope") != policy.execution_scope
            or situation.get("execution_scope") != policy.execution_scope
        ):
            reasons.append("policy_execution_scope_mismatch")
        contract = situation.get("mission_contract", {}).get("mission_context", {})
        if (
            digest(contract) != policy.mission_contract_sha256
            or facts.get("mission_contract_sha256") != policy.mission_contract_sha256
        ):
            reasons.append("policy_mission_contract_changed")
        try:
            age = (datetime.now(timezone.utc) - timestamp(facts["observed_at"])).total_seconds()
            if not 0 <= age <= policy.max_observation_age_seconds or not facts.get("source_ref"):
                reasons.append("policy_fresh_observation_required")
        except (KeyError, ValueError, TypeError):
            reasons.append("policy_fresh_observation_required")
        if any(facts.get("predicates", {}).get(key) is not True for key in policy.preserve):
            reasons.append("policy_preserve_condition_not_verified")
        action = request.get("recovery_action")
        parameters = request.get("recovery_parameters", {})
        judged = graph.get("recovery_result", {}).get("assessment", {})
        if judged.get("proposed_parameters") != parameters:
            reasons.append("policy_judged_parameters_mismatch")
        if graph.get("recovery_proposed_action") != action:
            reasons.append("policy_judged_action_mismatch")
        rule = policy.actions.get(action)
        if rule is None:
            reasons.append("policy_action_out_of_scope")
        else:
            variants = rule.parameter_variants or [rule.parameters]
            matching = [
                variant
                for variant in variants
                if isinstance(parameters, dict) and set(parameters) == set(variant)
            ]
            if not matching:
                reasons.append("policy_parameter_set_mismatch")
            elif not any(
                all(parameter_matches(bound, parameters[key]) for key, bound in variant.items())
                for variant in matching
            ):
                for key, bound in matching[0].items():
                    if not parameter_matches(bound, parameters[key]):
                        reasons.append("policy_parameter_out_of_range:" + key)
        proposal = request.get("proposal_id")
        if not isinstance(proposal, str) or not proposal.strip():
            reasons.append("policy_proposal_id_required")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT revoked,approved_by,approved_at FROM policies WHERE sha=?", (sha,)
            ).fetchone()
            if not row or row[0]:
                reasons.append("policy_revoked")
            if timestamp(policy.expires_at) <= datetime.now(timezone.utc):
                reasons.append("policy_expired")
            total = db.execute(
                "SELECT COUNT(*) FROM reservations WHERE mission=?", (policy.mission_id,)
            ).fetchone()[0]
            used = db.execute(
                "SELECT COUNT(*) FROM reservations WHERE mission=? AND action=?",
                (policy.mission_id, action),
            ).fetchone()[0]
            if total >= policy.max_total_actions or (rule and used >= rule.max_uses):
                reasons.append("policy_budget_exhausted")
            if db.execute(
                "SELECT 1 FROM reservations WHERE mission=? AND proposal=?",
                (policy.mission_id, proposal),
            ).fetchone():
                reasons.append("policy_proposal_already_reserved")
            reserved = consume and not reasons and policy.mode == "bounded"
            if reserved:
                db.execute(
                    "INSERT INTO reservations VALUES(?,?,?,?,?)",
                    (policy.mission_id, proposal, action, sha, digest(request)),
                )
        return {
            "policy_sha256": sha,
            "mode": policy.mode,
            "policy_approval": {"approved_by": row[1], "approved_at": row[2]} if row else {},
            "authority_source": "human_approved_policy",
            "policy_eligible": not reasons,
            "policy_authorized": reserved,
            "budget_reserved": reserved,
            "individual_human_approval_observed": False,
            "blocking_reasons": list(dict.fromkeys(reasons)),
        }

    def handler(self, sha: str, observe):
        """Inject from trusted composition code, never from model/request JSON."""

        def check(state):
            return self.check(
                sha,
                graph=state["frozen_mission_incident_graph"],
                request=state["continuation_request"],
                facts=observe(),
                consume=state.get("policy_check_phase") == "before_executor",
            )

        return check
