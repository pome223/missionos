# VLA-0 diagnostic-clone supervisory Recovery

This contract covers one bounded MissionOS Repair attempt against a restored
LIBERO MuJoCo state. It is diagnostic simulator evidence, not live same-world
continuity or physical execution.

## Authority and sequence

```text
Verifier detects predicate failure.
MissionOS selects a predicate-bound semantic Repair intent.
Human approval binds that proposal.
Rules admit one bounded dispatch.
VLA-0 generates low-level policy actions.
Verifier observes predicate transitions.
Verifier applies a 20-step stationary hold after first conjunction.
Verifier records stable or unstable as the final verdict.
```

The authority split remains:

```text
LLM judges.
Human approves.
Rules constrain.
Executor acts.
Verifier checks.
Repair loops.
```

Selection is deterministic and predicate-driven. For the moka-pot fixture,
`semantic_preserve` names the failed target placement and the predicates that
must remain satisfied. The proposal records the failed and preserved predicate
ids, exact instruction, instruction digest, and that the runtime instruction
was neither free-form human input nor a low-level action.

## Policy and verifier actions

Before predicate conjunction, every applied 7D action is selected from a fresh
VLA-0 inference. MissionOS does not generate, modify, or replace that policy
trajectory.

The first conjunction is only entry into stability verification. The verifier
then applies up to 20 exact stationary actions:

```text
[0, 0, 0, 0, 0, 0, -1]
```

No VLA-0 inference is permitted during this hold. Policy actions and verifier
hold actions have separate counts and traces, while both consume the same
Contract-bound simulator-action budget. The hold stops on predicate regression,
preservation regression, or preservation-invariant breach.

`stable` requires all 20 hold observations to retain the predicate conjunction
and preservation limits. A transient conjunction followed by regression is
`unstable_after_predicate_conjunction`, not completion.

## Required trace material

The digest-bound report must retain:

- source predicate observations and the detected failed/preserved ids;
- selected Repair intent, exact instruction, and VLA-0 payload readback;
- proposal, approval, dispatch, and single-use dispatch receipt;
- model-inference count and each applied policy action;
- predicate and preservation observations after each simulator action;
- first conjunction action, verifier hold trace, and hold-action digest;
- policy-action, verifier-hold, and total simulator-action counts;
- stable steps required and observed, final predicates, and final verdict.

## Five-axis runtime diagnostic producer

`run_vla0_libero_snapshot_recovery.py` projects the same attempt into
`missionos_core_repair_diagnostic_report.v1`. It does not recalculate or create
authority in Core. The VLA-0 adapter owns these criteria:

| Axis | Adapter criterion | Provenance |
| --- | --- | --- |
| action activity | at least one applied six-dimensional arm command has norm at least `1e-4`; the gripper dimension is excluded | digest-bound applied 7D action trace and policy step range |
| corrective alignment | the EEF approaches the failed target by at least `0.01 m`, or target/gripper contact is observed | per-step target witness trace |
| predicate recovery | the actual LIBERO goal conjunction is observed at least once | per-step simulator predicate trace |
| preservation | no preserved predicate transition or registered continuous invariant breach is observed | full policy and verifier-hold trace |
| stable hold | the verifier-owned post-conjunction hold is admitted and retains the conjunction for all 20 required steps | hold-action digest and contiguous hold step range |

Every axis uses one dispatch-bound `observation_scope_ref`. Criterion material
is stored with its canonical digest, and evidence references identify the
supporting digest and collection range. If the predicate conjunction is never
observed, the stable-hold axis is `not_observed`, not failed. Approval,
dispatch, completion, general executor capability, and physical execution
fields remain false in the diagnostic report.

## Claim boundary

A positive diagnostic-clone verdict establishes only that the tested VLA-0
policy actions entered the required predicates and that the verifier observed
them remain satisfied during the bounded hold in the restored simulator state.
It does not establish live same-world Semantic Repair, autonomous approval,
independent controller ACK, physical execution, a general recovery rate, or
real-world safety.

## Recorded 3 cm run

The 2026-08-29 seed-0 run exercised the integrated path with the predicate-
selected `semantic_preserve` instruction. VLA-0 received the exact selected
instruction on all 128 policy calls, but produced no gripper contact and only
`1.007026553366761e-9` metres of target translation. The predicate vector
remained `[true, false, true]`.

The final verdict was `predicate_not_reached`. Because conjunction was never
observed, the verifier correctly admitted zero hold actions; this is not a
20-step stability result. The bounded public record is
[`evidence/20260829-vla0-libero-supervisory-loop.json`](evidence/20260829-vla0-libero-supervisory-loop.json).
