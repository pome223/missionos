"""Exact-point prerequisite for the normal return permission entry.

This additive adapter leaves the flown plant and experimental entry untouched.
It checks evidence BEFORE calling the owning service's ordinary permission
entry; the certificate never supplies human approval or an execution grant.
Callbacks here are trusted host code, never model/JSON supplied functions.
"""
from dataclasses import asdict
from hashlib import sha256

from scripts import starship_final_qualification_chunks as chunks
from . import starship_final_qualification as legacy
from . import starship_final_shared as shared
from .starship_final_handoff import FinalActualReturnHandoff

EXTENSIONS = frozenset({'verification_registration_sha256', 'verification_source_commit',
    'verification_source_sha256', 'verification_backend'})
_SEAL = object()


class VerifiedReturnCertificate:
    """Prepare once from all raw records and a trusted independent checker.

    Preparation is outside the live observation epoch. No cached passed flag
    substitutes for verify_record. The immutable rebuilt preimages stay bound
    to original flown sources; this adapter is separately source-pinned.
    """
    __slots__ = ('_core', '_certificate_sha256', '_backend', '_checker_source', '_adapter_source')

    def __init__(self, *, _seal=None, core=None, raw_sha=None, backend=None,
                 checker_source=None, adapter_source=None):
        legacy._require(_seal is _SEAL, 'qualified_return_requires_independently_prepared_certificate')
        self._core = legacy.canonical_bytes(core)
        self._certificate_sha256 = raw_sha
        self._backend = legacy.canonical_bytes(backend)
        self._checker_source = legacy.canonical_bytes(checker_source)
        self._adapter_source = legacy.canonical_bytes(adapter_source)

    @classmethod
    def prepare(cls, certificate_raw, *, expected_sha256, expected_identity, required_cells,
                records_raw, verify_record, expected_adapter_source):
        legacy._require(type(certificate_raw) is bytes and len(certificate_raw) <= chunks.MAXIMUM_POINT_BYTES
            and sha256(certificate_raw).hexdigest() == expected_sha256,
            'qualified_return_certificate_bytes_or_pin')
        c = shared.parse(certificate_raw)
        rebuilt = chunks.build_certificate(expected_identity=expected_identity,
            required_cells=required_cells, records_raw=records_raw, verify_record=verify_record)
        legacy._require(type(c) is dict and set(c) == set(rebuilt) | EXTENSIONS
            and legacy.canonical_bytes({k:c[k] for k in rebuilt}) == legacy.canonical_bytes(rebuilt)
            and rebuilt['qualification_complete'] is True,
            'qualified_return_certificate_not_independently_rebuilt')
        legacy._require(type(expected_adapter_source) is dict
            and {k for k in expected_adapter_source if k.startswith('src/')}
                == {'src/runtime/starship_qualified_return.py'},
            'qualified_return_adapter_source_pin_required')
        legacy._require(legacy._hash(c['verification_registration_sha256'])
            and type(c['verification_source_commit']) is str and len(c['verification_source_commit']) == 40,
            'qualified_return_verification_identity')
        value = cls(_seal=_SEAL, core=rebuilt, raw_sha=expected_sha256,
            backend=c['verification_backend'], checker_source=c['verification_source_sha256'],
            adapter_source=expected_adapter_source)
        value.check_environment()
        return value

    def check_environment(self):
        c = shared.parse(self._core)
        for manifest in (c['identity']['source_sha256'], shared.parse(self._checker_source),
                         shared.parse(self._adapter_source)):
            shared.check_manifest(manifest)
        legacy._require(shared.environment() == shared.parse(self._backend),
            'qualified_return_current_backend_mismatch')
        backend = shared.parse(self._backend)
        legacy._require(c['identity']['backend'] == {k:backend[k] for k in c['identity']['backend']},
            'qualified_return_certificate_backend_mismatch')
        return c

    def current_source_allowed(self, current):
        core = self.check_environment()
        original = core['identity']['source_sha256']
        extra = {k:v for k,v in shared.parse(self._adapter_source).items() if k.startswith('src/')}
        legacy._require(not (set(original) & set(extra)), 'qualified_return_adapter_cannot_replace_certified_source')
        return current == original or current == {**original, **extra}

    def match_preimages(self, *, point_data, domain, identity):
        """Evidence only. Public saved-replay callers cannot mint a live owner."""
        core = self.check_environment()
        legacy._require(legacy._identity(identity) == core['identity'], 'qualified_return_current_identity_mismatch')
        d, point = legacy._domain(domain), chunks.make_point(point_data)
        matches = [r['cell_id'] for r in core['cells']
            if r['cell_passed'] and r['domain'] == d and r['point'] == point]
        return {'schema':'missionos.starship_current_qualified_return_match.v1',
            'point_domain_evidence_matched': bool(matches), 'matched_cell_ids': matches,
            'certificate_sha256': self._certificate_sha256, 'identity_sha256': legacy.digest(identity),
            'point_sha256': legacy.digest(point), 'domain_sha256': legacy.digest(d),
            'history_rows':point['history']['row_count'],
            'reason':None if matches else 'unsampled_point_or_continuation_domain',
            'qualification_scope':'ship_return_exact_model_points_only',
            'continuous_uncertainty_envelope_verified':False, 'old39_certificate_admitted':False,
            'human_approval_inferred':False, 'dispatch_authorized':False,
            'physical_execution_invoked':False, 'runtime_execution_inferred_from_artifact':False}


def _actual(handoff):
    """Re-read the sealed held objects, not merely its serialized snapshot.

    The one-use owner exposes no public peek; this trusted adapter deliberately
    checks its held references without consuming them. The existing consume()
    must check them again before any continuation can obtain ownership.
    """
    legacy._require(type(handoff) is FinalActualReturnHandoff and not handoff.consumed,
        'qualified_return_live_unconsumed_owner_required')
    h = handoff.record()
    legacy._require(legacy.canonical_bytes(asdict(handoff._state)) == legacy.canonical_bytes(h['snapshot']['state'])
        and legacy.canonical_bytes(asdict(handoff._vehicle)) == legacy.canonical_bytes(h['snapshot']['vehicle']),
        'qualified_return_actual_objects_changed')
    rows = h['coast_history']
    legacy._require(type(rows) is list and rows and len(rows) <= chunks.MAXIMUM_HISTORY_ROWS
        and all(type(r) is dict and set(r) == {'state','retained_count'}
            and r['retained_count'] == h['inventory']['retained_count'] for r in rows),
        'qualified_return_actual_history_inventory')
    b = h['snapshot']['binding']
    legacy._require(h['dispatch']['gate_met'] is True and h['dispatch']['refusal_reason'] is None
        and h['dispatch']['admission']['accepted'] is True
        and h['dispatch']['admission']['stage'] == 'dispatch'
        and b['retained_count'] == h['inventory']['retained_count'],
        'qualified_return_actual_dispatch_boundary')
    return h, {'state':shared.parse(legacy.canonical_bytes(asdict(handoff._state))),
        'vehicle':shared.parse(legacy.canonical_bytes(asdict(handoff._vehicle))),
        'inventory':h['inventory'], 'actuator_history':[
            {k:r['state'][k] for k in ('time_s','engine_states','flap_angles_rad')} for r in rows]}


class NormalReturnPermissionEntry:
    """Mandatory exact evidence gate around a host's existing permission entry.

    permission_entry owns authentic human approval, bounds and one-use dispatch.
    No boolean or serialized grant is accepted here instead of that code path.
    Calls are one-shot even if the downstream permission entry refuses/raises.
    Construct this at the normal return boundary, not at launch readiness. The
    unchanged legacy launch gate still refuses its missing 39-case certificate.
    """
    __slots__ = ('_certificate', '_domain', '_identity', '_entry', '_attempted', '_receipt')

    def __init__(self, certificate, *, continuation_domain, current_identity, permission_entry):
        legacy._require(type(certificate) is VerifiedReturnCertificate and callable(permission_entry),
            'qualified_return_trusted_certificate_and_permission_entry_required')
        self._certificate = certificate
        self._domain, self._identity = legacy.canonical_bytes(legacy._domain(continuation_domain)), legacy.canonical_bytes(legacy._identity(current_identity))
        self._entry, self._attempted, self._receipt = permission_entry, False, None

    def enter(self, handoff, *, current_plan):
        legacy._require(not self._attempted, 'qualified_return_permission_attempt_already_consumed')
        self._attempted = True
        h, point = _actual(handoff)
        from .starship_final_plan import verification_inputs
        verification_inputs(current_plan)
        legacy._require(h['policy']['plan_sha256'] == current_plan['plan_sha256']
            and self._certificate.current_source_allowed(current_plan['source_sha256'])
            and legacy.digest(current_plan['configuration']) == shared.parse(self._identity)['integration_policy_sha256'],
            'qualified_return_current_plan_or_configuration_mismatch')
        domain, identity = shared.parse(self._domain), shared.parse(self._identity)
        b = h['snapshot']['binding']
        legacy._require(h['origin']['source_sha256'] == current_plan['source_sha256']
            and b['profile_sha256'] == identity['profile_sha256']
            and b['candidate_id'] == domain['return_candidate_id'],
            'qualified_return_owner_source_profile_or_candidate_mismatch')
        # A historical post-dispatch numerical application is not known at the
        # pre-fault permission epoch. Do not treat it as current knowledge.
        # Only the already-frozen no-additional-return-fault continuation can
        # presently use the ordinary entry. Fault cells remain evidence only.
        legacy._require(domain['fault_family'] == 'nominal'
            and domain['controller_capability_factor'] == 1.
            and current_plan['case_id'] == 'N'
            and current_plan['composition']['fault_selection']['selected_family'] == 'normal',
            'qualified_return_fault_continuation_not_known_at_permission_epoch')
        receipt = self._certificate.match_preimages(point_data=point, domain=domain, identity=identity)
        receipt['current_plan_sha256'] = current_plan['plan_sha256']
        receipt['current_owner_source_sha256'] = legacy.digest(current_plan['source_sha256'])
        self._receipt = receipt
        legacy._require(receipt['point_domain_evidence_matched'], 'qualified_return_unsampled_current_point')
        # Nothing has consumed the actor, advanced physics or inferred approval.
        # Only now may the existing owning service perform its permission work.
        return self._entry(handoff, qualification_receipt=legacy._copy(receipt))

    def record(self):
        return {'schema':'missionos.starship_normal_return_permission_entry.v1',
            'attempt_consumed': self._attempted, 'qualification':legacy._copy(self._receipt),
            'qualification_is_execution_permission':False, 'old39_certificate_admitted':False}
