"""Typed numerical admission behind a stable, candidate-bound scope request.

An operator's request names a fixed proposal, while new observations, notices,
resources and forecast records receive new proof hashes. This bridge checks
the exact typed fresh inputs before returning that original proposal identity
to the existing approval actor. It verifies no human identity or grant, changes
no reservation and grants no dispatch or old-certificate qualification.
"""
from copy import deepcopy
from dataclasses import dataclass, field
import json

from . import starship_restored_admission as numerical
from .starship_restored_approval import RestoredApprovalBinding

_ACTIONS = {"select_next_orbit": "next_orbit", "keep_reserved_return": "nominal"}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@dataclass(frozen=True, init=False)
class RestoredApprovalAdmissionBridge:
    """One original proposal per action, fresh exact typed tool inputs per check.

    ``current_inputs(action, state)`` is a trusted worker factory, not a model or
    an authority callback. It returns exactly ``(RestoredCandidate,
    RestoredCurrentObservation, RestoredOperationalNotice, RestoredResourceState)``.
    The factory may rebind a forecast to new notice/resource generations or
    calculate a fresh forecast from actual coast, but cannot retime the fixed
    proposal. ``keep_reserved_return`` always checks the nominal candidate.
    """

    _binding_json: bytes = field(repr=False)
    _proposals_json: bytes = field(repr=False)
    _current_inputs: object = field(repr=False, compare=False)

    def __init__(self, binding, next_candidate, nominal_candidate, *, current_inputs):
        _require(type(binding) is RestoredApprovalBinding
            and type(next_candidate) is numerical.RestoredCandidate
            and type(nominal_candidate) is numerical.RestoredCandidate and callable(current_inputs),
            "restored_approval_admission_requires_exact_typed_inputs")
        bound = binding.record()
        proposals = {}
        rows = {"select_next_orbit": next_candidate.record(), "keep_reserved_return": nominal_candidate.record()}
        for action, row in rows.items():
            proposal, anchor = row["proposal"], row["proposal"]["opportunity_anchor"]
            candidate_id = _ACTIONS[action]
            _require(row["proposal_sha256"] == numerical._hash(proposal)
                and proposal["candidate_id"] == candidate_id
                and proposal["return_time_s"] == bound[candidate_id+"_burn_time_s"]
                and proposal["source_sha256"] == numerical._hash(bound["source_sha256"])
                and proposal["profile_sha256"] == bound["profile_sha256"]
                and proposal["inventory_receipt_chain_sha256"] == bound["inventory_receipt_chain_sha256"]
                and proposal["release_generation"] == bound["release_generation"]
                and proposal["retained_count"] == bound["retained_count"]
                and anchor["origin_sha256"] == bound["restored_origin_sha256"]
                and anchor["source_record_sha256"] == bound["source_record_sha256"]
                and anchor["actual_cutoff_time_s"] == bound["actual_cutoff_time_s"]
                and anchor["nominal_s"] == bound["nominal_burn_time_s"]
                and anchor["next_orbit_s"] == bound["next_orbit_burn_time_s"],
                "restored_approval_proposal_does_not_match_frozen_binding")
            proposals[action] = {"proposal": proposal, "proposal_sha256": row["proposal_sha256"]}
        first, second = (proposals[action]["proposal"] for action in _ACTIONS)
        _require(all(_wire(first[key]) == _wire(second[key]) for key in (
            "opportunity_anchor", "source_sha256", "profile_sha256", "inventory_receipt_chain_sha256",
            "release_generation", "retained_count", "controller_policy", "admission_policy_sha256",
            "site_policy_sha256")), "restored_approval_proposals_have_different_registered_scope")
        object.__setattr__(self, "_binding_json", _wire(bound))
        object.__setattr__(self, "_proposals_json", _wire(proposals))
        object.__setattr__(self, "_current_inputs", current_inputs)

    def proposal_sha256(self, action):
        _require(type(action) is str and action in _ACTIONS, "restored_approval_admission_action_outside_scope")
        return json.loads(self._proposals_json)[action]["proposal_sha256"]

    def __call__(self, action, state):
        self.proposal_sha256(action)
        bound = json.loads(self._binding_json)
        _require(type(state) is dict and state.get("run_active") is True
            and numerical._number(state.get("simulation_time_s"))
            and state["simulation_time_s"] >= bound["closed_origin_time_s"]
            and state.get("restored_origin_sha256") == bound["restored_origin_sha256"],
            "restored_approval_admission_requires_current_worker_state")
        inputs = self._current_inputs(action, deepcopy(state))
        _require(type(inputs) is tuple and len(inputs) == 4
            and type(inputs[0]) is numerical.RestoredCandidate
            and type(inputs[1]) is numerical.RestoredCurrentObservation
            and type(inputs[2]) is numerical.RestoredOperationalNotice
            and type(inputs[3]) is numerical.RestoredResourceState,
            "restored_approval_admission_factory_returned_untyped_inputs")
        candidate, observation, notice, resources = inputs
        selected, current = candidate.record(), observation.record()
        frozen = json.loads(self._proposals_json)[action]
        _require(_wire(selected["proposal"]) == _wire(frozen["proposal"])
            and selected["proposal_sha256"] == frozen["proposal_sha256"],
            "restored_approval_fresh_proof_changed_approved_proposal")
        _require(current["state"]["time_s"] == state["simulation_time_s"]
            and current["closed_origin_sha256"] == bound["restored_origin_sha256"]
            and _wire(current["source_sha256"]) == _wire(bound["source_sha256"])
            and current["profile_sha256"] == bound["profile_sha256"]
            and current["inventory_receipt_chain_sha256"] == bound["inventory_receipt_chain_sha256"]
            and current["release_generation"] == bound["release_generation"]
            and current["retained_count"] == bound["retained_count"],
            "restored_approval_actual_observation_rebound_or_stale")
        admission = numerical.admit_restored_candidate(candidate, observation,
            notice=notice, resources=resources, stage="selection")
        _require(admission["proposal_sha256"] == frozen["proposal_sha256"],
            "restored_approval_numerical_result_changed_proposal")
        return {"schema": "missionos.starship_restored_approval_numerical_admission.v1",
            "accepted": admission["accepted"], "action": action,
            "candidate_sha256": frozen["proposal_sha256"],
            "proposal_sha256": frozen["proposal_sha256"],
            "current_candidate_sha256": selected["candidate_sha256"],
            "forecast_proof_sha256": selected["forecast_proof_sha256"],
            "numerical_admission": admission,
            "candidate_identity_semantics": "stable_bound_proposal_not_mutable_forecast_proof",
            "operator_approval_verified": False, "scope_amendment_applied": False,
            "mission_authority_granted": False, "dispatch_authorized": False,
            "reservation_changed": False, "physical_state_modified": False}

    def record(self):
        return {"schema": "missionos.starship_restored_approval_numerical_bridge.v1",
            "binding_sha256": numerical._hash(json.loads(self._binding_json)),
            "action_proposals": json.loads(self._proposals_json),
            "fresh_exact_typed_inputs_required": True,
            "operator_approval_verified": False, "mission_authority_granted": False,
            "dispatch_authorized": False, "reservation_changed": False}
