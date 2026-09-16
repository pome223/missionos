# Causal repair candidate generation before selection

Status: implementation and fixture validation only. No new robotics outcome
matrix has been collected. This does not establish candidate quality, added
recovery, superiority to an incumbent repair, or readiness for Phase B.

## Scope and authority

`src/runtime/causal_repair_candidates.py` compiles caller-supplied family
proposals into bounded, immutable primitive programs. There are no changes to
WAM, experience ordering, or a deployed selector, and no new model calls.

LLM judges. Human approves. Rules constrain. Executor acts. Verifier checks.
Family proposals and hypothesis references are advisory input; a nonempty
reference does not prove the hypothesis. The caller supplies observed state,
coordinate bounds, primitive capabilities and a whole-program feasibility
check. Acceptance by this compiler is not approval or permission to execute.
The evaluation driver is for separately authorized resettable research trials,
not operational dispatch or hardware. It cannot supply approval for an adapter.

## Primitive contract

All targets are world-frame metres for the manipulated object's desired pose
position, not raw end-effector deltas. The adapter must translate these into
closed-loop control while preserving its registered orientation constraints.
Every primitive has a maximum number of control steps. The adapter must enforce
both primitive and whole-program limits and monitor preservation throughout.

| Family | Intervention variable | Prefix before common terminal operations |
| --- | --- | --- |
| `direct` | transport path, fixed simple baseline | Translate to goal |
| `increase_clearance` | vertical clearance | Raise above both entry and goal, translate, lower |
| `lateral_detour` | lateral corridor | Shift laterally, transport along that corridor, return |
| `release_and_reacquire` | grasp side | Place at staging region, release, acquire from opposite side, transport |

Every prefix ends with `place_then_stabilize(goal)`, `release(goal)`, and
`hold_and_observe(goal)`. Placement must be supported and stable before release.
Acquisition must confirm the new grasp before transport. `hold_and_observe`
must preserve the released state, not close the gripper again. An adapter that
cannot implement these semantics must omit the primitive from its capabilities.
Unsupported regrasp is rejected, never translated into an equivalent transport.

The initial compiler supports these three repair families plus direct transport.
It does not yet implement obstacle relocation or arbitrary free-text programs.
Protected objects cannot be direct manipulation targets. Workspace and budget
checks are necessary only: the caller's feasibility callback must check swept
geometry, protected objects, grasp and staging feasibility. Unknown feasibility
rejects the candidate. Even a feasible program still requires normal authority
checks before operational execution.

## Phase A protocol

`src/runtime/candidate_informativeness.py` provides a backend-neutral exhaustive
runner and evaluator. Freeze the following before inspecting evaluation outcomes:

1. A development/evaluation split and 50–100 unused, distinct start snapshots.
   Record cohort construction and its task-distribution limitations. Do not select
   evaluation starts based on observed candidate gains. Include incumbent-success
   controls. Development data may be used to choose families and parameters.
2. The generated programs, rejection reasons, caller-owned feasibility rules,
   full simulator/controller/RNG snapshots, and source/backend versions. Store
   the serialized `EvaluationPlan` and its `plan_digest` before executing trials.
   A digest binds content; it alone does not prove prospective registration.
   `backend_contract_digest` binds simulator, controller, primitive and Verifier
   versions and semantics; the adapter must match it before any reset or execution
   and include it in every observed receipt.
3. Three to five candidate families including a frozen fixed comparator. Include
   the existing registered repair as an additional fifth family where available;
   direct transport alone is not an incumbent-comparison result.
4. Identical whole-program control budgets, terminal predicate definitions,
   preservation tolerances, stabilization window and timeout semantics. An action
   timeout with known terminal observations is an observed failure; missing
   observations or infrastructure failures are unknown, never ordinary failure.
5. Admission thresholds. Initial defaults are at least 30% safe-success divergence,
   at least 10 percentage points oracle gain over the *best fixed family*, zero
   preservation violations, and no missing outcomes across at least 50 starts.
   These are design choices, not empirically established universal thresholds.

Restore each candidate from the full same-start snapshot, read back its digest,
execute the bounded program, and obtain terminal verification. Reset mismatch
prevents execution. Every admitted candidate runs, even after another succeeds.
Failures stay in the matrix; there are no adaptive replacements or retries.
The adapter owns reset completeness and measurement truth. Receipts are trusted
adapter input, not independent attestation or proof of physical behavior.

Keep starts with feasibility rejections in the frozen plan using `Start.rejected`.
A rejected arm has no terminal outcome and does not create outcome divergence.
It contributes no success to the corresponding fixed family. Never remove a
start merely because too few candidates survive filtering. Report rejection and
coverage rates together with success, especially if the fixed arm is rejected.

Record `success/failure`, preservation over the full trajectory, control steps,
minimum clearance, manipulated-object displacement and terminal predicate vector.
Also retain trajectories and individual protected-object displacement in the
adapter evidence for auditing the summary fields. Unknown or absent receipts
prevent aggregate headroom qualification; the original denominator remains.

## Metrics and interpretation

Safe success requires every terminal contract predicate plus preservation.

```text
candidate_informativeness = starts with differing observed safe success / all starts
oracle_success = starts with at least one observed safe success
fixed_success = safe successes of the prospectively fixed family
best_fixed_success = maximum total safe successes of any one family
oracle_gain_over_best_fixed = (oracle_success - best_fixed_success) / all starts
```

The evaluator also reports terminal predicate-vector divergence separately.
If one candidate always wins and another always fails, informativeness can be
100% while oracle gain over best fixed is zero. That is a reason to adopt or
investigate the fixed program, not evidence that a selector is needed. Preservation
losses cannot inflate safe success and any such loss blocks the default gate.

Completion steps are retained per trial; do not compare mean completion time
only among survivors. For an efficiency endpoint, freeze a failure penalty and
paired-budget analysis separately before the run. This version's admission gate
is solely about safe terminal success, not one-to-three-step differences.

The oracle is hindsight headroom, not a deployable policy. An exhaustive matrix
spends multiple trial budgets per start; it does not show that sequential retry
has the same cost as choosing one candidate. `fixed_success` means one frozen
family per start, not an unlimited fixed-order retry chain. Phase B must use the
same frozen candidate set and a matched single-execution budget, and compare a
current-state heuristic, experience, WAM, and their combination on unused starts.

## Runtime smoke

```sh
PYTHONPATH=. python3 -m scripts.smoke_causal_repair_candidates --fixture
PYTHONPATH=. python3 -m pytest -q tests/contract/test_causal_repair_candidates.py
```

Add `--output <new-directory>` to save the plan before execution and flush each
measurement to an append-only JSONL journal. Existing output directories are
rejected. An interruption leaves an incomplete, inspectable attempt rather than
silently replacing it on rerun.

The smoke exercises compilation, independent reset before all 12 fixture arms,
receipt binding, and exhaustive aggregation. Its outcomes are deliberately
scripted to test distinct mechanisms and arithmetic. `evidence_kind=fixture`
always prevents Phase B admission. Neither this smoke nor the tests run LIBERO,
robotics dynamics, a real LLM, production approval/dispatch or hardware.

To complete qualification, implement the above primitive and reset contract in
an opt-in robotics adapter and run the frozen cohort. Fixture results must never
be relabeled as simulator results. Until then, the research success condition
remains untested and this feature should remain a draft research implementation.
