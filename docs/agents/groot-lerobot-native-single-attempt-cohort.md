# GR00T N1.7 Native Single-Attempt Repair Cohort

This record publishes a bounded negative measurement from the governed
same-world Repair path. The post-observation-amended cohort completed at
`0/5 loops`: five naturally occurring asymmetric partial failures were each
given one approved Repair attempt, with no automatic retry, and none improved
the target predicate.

This was not the originally planned max-two-attempt experiment. The amended
single-attempt protocol was fixed after loop 1 and before native loops 2–5.
The max-two-attempt protocol remains `unmeasured_as_registered`.

## Native cohort result

The unit is a natural-failure loop, not an attempt or an action chunk. All five
artifacts were accepted by the same aggregator configuration:

```text
planned_loop_count = 5
max_attempts_per_loop = 1
automatic_retry_performed = false
primary_measurement = 0/5 loops
```

The aggregator fails closed unless every loop has the expected schema, at
least one indexed attempt, an explicit result mapping and status, an explicit
boolean preservation result, an explicit boolean Semantic Repair result, and
consistent loop/result terminal states. Missing evidence is not normalized
into a negative observation.

| Loop | Source vector | Repair target | Contract-bound preserve set | Result |
| ---- | ------------- | ------------- | --------------------------- | ------ |
| 1 | `[false, true, true]` | predicate 0 | predicates 1 and 2 | no improvement |
| 2 | `[false, true, true]` | predicate 0 | predicates 1 and 2 | no improvement |
| 3 | `[true, false, true]` | predicate 1 | predicates 0 and 2 | no improvement |
| 4 | `[true, false, true]` | predicate 1 | predicates 0 and 2 | no improvement |
| 5 | `[true, false, true]` | predicate 1 | predicates 0 and 2 | no improvement |

The direction was not held constant: two loops targeted predicate 0 and three
targeted predicate 1. The measurement therefore describes five naturally
selected asymmetric failures in either direction, not five repeats of one
fixed source vector.

## Preservation result

The earlier public record contained one original-world attempt and ten
diagnostic clones, with each execution's own Contract-bound preserve
predicates maintained `11/11`. The native cohort adds five original-world
attempts maintained `5/5`.

Together, the bounded record is:

```text
6 original-world attempts + 10 diagnostic clones = 16 executions
each execution's own Contract-bound preserve predicates maintained = 16/16
preservation violations observed = 0
```

This is not a claim that one identical predicate pair was preserved sixteen
times. The Repair target changes direction, so the preserve set changes with
the Contract. It is also not a real-world safety result.

## Authority and claim boundary

Proposal, human approval, dispatch, controller ACK, simulator effect,
predicate result, task completion, Semantic Repair, and physical execution
remain separate facts. The five loops establish simulator execution and a
negative target-predicate result; they do not turn a proposal or dispatch into
success.

This record establishes only the bounded observations above. It does not
establish:

- a general GR00T Repair success rate;
- a difference between same-world continuation and diagnostic clone re-entry;
- that the originally planned max-two-attempt protocol was measured;
- real-world safety or physical Panda execution.

The normalized cohort result is
[`20260820-groot-n17-lerobot-native-single-attempt-cohort-result.json`](evidence/20260820-groot-n17-lerobot-native-single-attempt-cohort-result.json).
The publication companion adds the per-loop target direction, combined
preservation denominator, and explicit claim boundary in
[`20260820-groot-n17-lerobot-native-single-attempt-cohort-publication.json`](evidence/20260820-groot-n17-lerobot-native-single-attempt-cohort-publication.json).

## Five-axis projection

The existing cohort has also been projected into the backend-neutral Repair
diagnostic contract without rerunning GR00T. For each loop, the publication
supports `predicate_recovery=not_satisfied` and `preservation=satisfied`.
It does not retain action-magnitude evidence, a preregistered corrective
alignment trace, or a post-conjunction hold window. Those axes therefore remain
`not_observed`; they are not inferred from the terminal predicate result.

The deterministic projection is
[`20260830-groot-n17-five-axis-repair-diagnostics.json`](evidence/20260830-groot-n17-five-axis-repair-diagnostics.json).
It binds the source cohort and publication by SHA-256, validates their internal
result digest and per-loop predicate partition, and creates no new approval,
dispatch, execution, capability, or physical-execution claim. Rebuild it with:

```bash
python scripts/project_groot_lerobot_repair_diagnostics.py \
  docs/agents/evidence/20260820-groot-n17-lerobot-native-single-attempt-cohort-result.json \
  docs/agents/evidence/20260820-groot-n17-lerobot-native-single-attempt-cohort-publication.json \
  --output docs/agents/evidence/20260830-groot-n17-five-axis-repair-diagnostics.json
```

Raw simulator logs, approval values, dispatch ledger entries, environment
session identifiers, credentials, and workstation-local paths are not
published.
