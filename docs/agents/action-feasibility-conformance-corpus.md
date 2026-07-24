# Action Feasibility conformance corpus

This maintainer fixture freezes the PX4 Action Feasibility behavior that must
survive the MissionOS Core extraction tracked by internal Epic #99.

The corpus is not a TaskStore export. It contains no private task record,
operator identity, credential, absolute workstation path, prompt, or model
response. Values are deterministic, anonymized semantic extracts that preserve
the safety-contract shape needed by the verifier.

## Source evidence

The source observations were reviewed in:

- internal PR #96 and
  `docs/agents/evidence/20260723-px4-action-feasibility-composite-deepseek-e2e.md`;
- public MissionOS `v0.1.0-rc.4`, which repeated the positive contract from a
  clean public checkout.

The positive case extracts these observations:

- matching thermal parameter SET/GET evidence;
- matching requested and materialized 0.5 kg simulator payload mass;
- a separately approved, same-task bounded OFFBOARD calibration with five
  samples;
- source-backed obstacle geometry and a verified candidate;
- hosted DeepSeek judgment without approval or dispatch authority;
- separate human approval;
- dispatch-time latest-telemetry, policy, and feasibility revalidation;
- runner ACK kept distinct from execution effect;
- target reach followed by AUTO resume;
- later simulator landing and disarm;
- no real delivery-completion or physical-execution claim.

The refusal cases extract contract and Gateway findings retained by PR #96:

| Case | Frozen result |
| --- | --- |
| missing obstacle geometry | `unverified` |
| missing thermal readback | `unverified` |
| missing same-task performance envelope | `unverified` |
| stale telemetry | `unverified` |
| telemetry cursor regression | `unverified` |
| active-policy drift | `unverified` |
| negative wind-control margin | `blocked` |

These are semantic replay cases. They do not claim that the source simulator is
running when the corpus is replayed.

## Truth boundary

Each case contains two separate blocks:

- `truth_boundary.artifact_truth` describes the deterministic fixture and the
  observations extracted into it;
- `truth_boundary.runtime_truth` distinguishes live runtime evidence from
  contract/Gateway fixture evidence and always requires
  `runtime_invoked_by_this_replay=false`.

Only the positive case and refusal conditions actually observed in a live run
set `source_runtime_evidence_available=true`. Geometry, cursor, and policy
negative cases that are proven by contracts or a Gateway smoke keep that field
false and provide `source_contract_evidence_refs` instead.

The verifier also returns all of the following as false:

```text
source_runtime_reexecuted
llm_invoked
approval_created
dispatch_authority_created
physical_execution_invoked
completion_claimed
progress_counted
```

The positive source run did contain separate proposal, human approval, dispatch,
ACK, observed effect, and terminal artifacts. Replaying their sanitized facts
does not recreate those authorities or effects.

## Schema and integrity

The manifest is:

```text
tests/golden/action_feasibility/px4_v1/manifest.json
```

Every case carries:

- source references;
- a complete telemetry cursor;
- policy reference and digest;
- observed and derived Hazard State facts;
- candidate source references;
- expected exact blocking and unverified reasons;
- verifier assumptions;
- a seven-stage authority/observation chain with distinct artifact references;
- a canonical case digest.

The manifest separately hashes each full case and carries its own canonical
digest. All hashes are verified before a case result is accepted.

## Publication sanitation

The verifier rejects:

- private `task_*` identifiers;
- TaskStore/database fields;
- absolute local paths;
- credentials and secret-like values;
- prompt or model-response text;
- owner/session identity fields.

Contract tests inject each class of forbidden material after resealing the case,
so sanitation cannot pass merely because an old integrity hash fails first.

## Commands

Regenerate deterministically:

```bash
PYTHONPATH=. .venv/bin/python \
  scripts/curate_action_feasibility_conformance_corpus.py
```

Run the maintained offline boundary:

```bash
PYTHONPATH=. .venv/bin/python \
  scripts/smoke_action_feasibility_conformance_corpus.py
```

Run focused contracts:

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/contract/test_action_feasibility_conformance_corpus.py
```

No command above needs network access, an LLM, PX4, Gazebo, an approval token, or
a runner.

## Next consumer

Issue #101 must consume this corpus from the new `missionos-core` conformance
API. Issue #102 must then make the PX4 adapter pass the same cases without a
parallel shadow schema. Until those imports and tests exist, this corpus is a
regression baseline, not proof that Core extraction is complete.
