# Anonymized Recovery Replay Bundle Contract

## Purpose

`missionos_anonymized_recovery_replay_bundle.v1` publishes a machine-readable
subset of one PX4/Gazebo task without publishing the private task database. The
bundle is evidence transport, not execution authority and not a verifier verdict
by itself.

## Production sources

The exporter consumes one serialized task record and reads only these artifact
families:

- `missionos_runtime_recovery_proposals`
- `missionos_runtime_recovery_dispatch_receipts`
- `missionos_runtime_recovery_dispatch_receipt` as a legacy latest-receipt fallback
- `missionos_runtime_recovery_attempts`
- `missionos_auto_mission_runtime_replay`
- route, dropoff, payload-release, delivery, and terminal monitor summaries

New PX4 Recovery dispatches append the immutable receipt to
`missionos_runtime_recovery_dispatch_receipts` while retaining the singular
latest-receipt view for compatibility. The collection key is
`dispatch_receipt_id`, derived from the canonical receipt SHA-256.

## Publication boundary

The exporter must omit:

- raw `task_id`, owner, and session values
- task databases and filesystem paths
- WGS84 latitude and longitude
- prompts and model response text
- credentials and local environment values

Telemetry is emitted only in local NED coordinates. The source task snapshot is
represented by a digest. That digest is an attestation and cannot be recomputed
without the private source record; the limitation is encoded in the bundle.

## Per-epoch chain

Each executed epoch contains:

```text
proposal
  recovery_intent (hash verified)
  intent_compilation (hash verified and intent-bound)
  reachability_verification (hash verified and compilation-bound)

approval_and_dispatch
  human approval payload or explicit reference-only limitation
  proposal revalidation
  bounded action and parameters
  dispatch authority fact

observation
  ACK observation
  executor-effect observation
  target-reached observation
  resume-safety verification
  recovery-outcome artifact and hash
```

The verifier rejects mixed intent/compilation/reachability chains, proposal and
observation mismatches, absent executor effect, target non-arrival, unsafe AUTO
resume, overclaimed delivery, physical-execution claims, bundle tampering, and
publication-boundary violations.

ACK remains distinct from executor effect:

```text
command_ack_observed=true
ack_is_execution_effect=false
executor_effect_observed=true  # required separately
```

## Commands

Export a task JSON record:

```bash
python scripts/missionos_recovery_replay_bundle.py export \
  --task-json private-task-record.json \
  --public-run-ref publication-safe-run-name \
  --max-telemetry-samples 240 \
  --output replay-bundle.json
```

Verify a bundle:

```bash
python scripts/missionos_recovery_replay_bundle.py verify \
  --bundle replay-bundle.json \
  --output replay-verdict.json
```

`verified_with_limitations` is not equivalent to `verified`. In particular, a
`reference_only` approval binding means the source task predates historical
receipt preservation. It must remain visible in `limitations`.

The verifier itself sets `progress_counted=false`. It reports the causal form of
the evidence but does not create approval, dispatch authority, mission progress,
delivery completion, or physical authority.
