# Repair Diagnostic Contract

## Purpose

`missionos_core_repair_diagnostic_report.v1` separates five stages that are
often collapsed into one Repair result. The evaluator is backend-neutral and
does not generate approval, dispatch, completion, capability, or physical
execution claims.

## Required axes

Every valid report contains exactly one observation for every axis, in this
order:

| Axis | Question | Accepted evidence basis |
| --- | --- | --- |
| `action_activity` | Did the executor emit a meaningful action? | model output, diagnostic reference, or observed effect |
| `corrective_alignment` | Was the action aligned with a preregistered corrective reference? | diagnostic reference or observed effect |
| `predicate_recovery` | Did the missing predicate become true? | simulator, external-runtime, or physical observation |
| `preservation` | Did protected predicates remain true? | simulator, external-runtime, or physical observation |
| `stable_hold` | Did the conjunction remain true for the required hold period? | simulator, external-runtime, or physical observation |

Each observation carries:

- `criterion_ref`: the preregistered rule used to classify the observation
- `observation_scope_ref`: the exact run, snapshot, or runtime scope
- `evidence_refs`: references to inspectable evidence
- `measurements`: backend-neutral scalar or structured measurements

Core does not generate an oracle waypoint, transform controller coordinates,
or compute an alignment score. The backend adapter or diagnostic evaluator owns
that calculation and supplies the resulting status, measurements, criterion
reference, and evidence references. Core validates their evidence basis and
scope binding without adopting a waypoint representation.

Runtime producers should use digest-bound evidence references that locate the
supporting material, for example:

```text
sha256:<digest>#/raw_action_trace/0:128
sha256:<digest>#/post_conjunction_stability/trace/0:20
```

The criterion material must be retained in `measurements` and its canonical
digest used as `criterion_ref`. Step ranges are inclusive in measurements and
use one-based simulator step numbers; JSON-pointer-like evidence ranges use the
stored collection's zero-based half-open indices.

The status is `satisfied`, `not_satisfied`, or `not_observed`. A
`not_observed` axis must use the `not_observed` evidence basis and must not cite
evidence. An observed status must cite at least one evidence reference.
All five axes must use the same `observation_scope_ref`; evidence from separate
runs cannot be combined into one assessment.

Historical evidence may be projected into this schema, but a projection must
not silently turn an absent measurement into a failure or a pass. A missing
action trace, alignment reference, or hold window remains `not_observed`. The
projector must bind its source artifacts by digest and state that it performed
no fresh execution or inference.

## Failure classification

The evaluator reports the first failed axis only when every preceding axis is
satisfied. Later observations are retained, but they do not change the first
failure stage. If no axis fails but a required axis is unobserved,
`next_unobserved_axis` identifies the first missing stage.

`stable_hold` additionally requires integer `required_hold_steps > 0` and
`observed_hold_steps >= 0`. A satisfied hold must meet the required count; a
failed hold must fall short of it.

## Scope of the diagnostic

This contract classifies an attempted Repair; it is not a Repair generator,
candidate selector, retry policy, or Repair-of-Repair mechanism. In particular,
a transient predicate conjunction must not be promoted to a successful Repair
when preservation or stable hold is not satisfied. A new executor, training
procedure, or recovery strategy requires a separately registered hypothesis,
fixture, and evaluation plan rather than being implied by a diagnostic result.

## Authority and claim boundary

Even when all five axes are satisfied,
`bounded_stable_repair_observed=true` means only that the report's exact
executor, task, fixture, and evaluation scope met the registered criteria. The
following fields remain pinned to `false`:

- `approval_created`
- `dispatch_authority_created`
- `dispatch_request_sent`
- `mission_completion_claimed`
- `executor_repair_capability_established`
- `physical_execution_invoked`

This preserves the MissionOS split:

```text
LLM judges.
Human approves.
Rules constrain.
Executor acts.
Verifier checks.
Repair loops.
```

## Example interpretation

If action activity is satisfied but corrective alignment is not satisfied, the
first failure stage is `corrective_alignment`. That means the executor moved but
did not produce a direction that met the registered corrective criterion. A
later preservation violation remains evidence in the report; it is not promoted
to mission failure, safety proof, or a general executor claim.

## Runtime verification

PR verification must exercise the exported `missionos_core` API, not only the
unit-level helpers. Construct all five observations, call
`evaluate_repair_diagnostics`, serialize the result, and verify both the failure
stage and the authority fields.
