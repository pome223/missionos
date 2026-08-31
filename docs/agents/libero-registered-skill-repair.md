# LIBERO registered-skill Repair contract

## Purpose

This boundary allows a deterministic skill registry to execute a Repair without
being represented as model inference. It preserves the authority sequence:

```text
actual predicate observation
-> exact residual-to-skill match
-> proposal
-> human approval
-> single-use dispatch
-> registered skill execution
-> per-step predicate and preservation verification
-> verifier-owned stable hold
```

The registered skill binding is digest-bound to the proposal and Repair
Contract. It records the skill id, exact-match selection basis, privileged-state
requirement, `model_inference_required=false`, and the completion basis. A
registered skill may continue after first predicate conjunction only until it
emits its bound completion signal. Preservation remains evaluated after every
simulator action. Learned-model adapters retain their existing stop behavior.

## Preservation rule

Registered deterministic skills use the strict displacement invariant:

- the protected-object reference pose and 5 mm limit are approved contract
  material;
- direct gripper contact is not required to detect a breach;
- indirect collision therefore stops the dispatch before the next skill step;
- the threshold is not changed after observing the result.

Learned-policy contracts retain the prior contact-qualified behavior for
backward compatibility.

## Runtime exercise

The 2026-08-31 CPU LIBERO exercise used task 8, init 15, seed 0 and a freshly
generated 3 cm `[true, false, true]` fixture. The governed run observed:

- exact registered-skill selection and human approval;
- one single-use dispatch receipt;
- no model inference;
- first `[true, true, true]` conjunction after action 33;
- protected-object displacement of 5.441 mm after action 58 without direct
  gripper contact;
- fail-closed status `stopped_on_preservation_invariant`;
- zero verifier-hold actions and no completion claim.

The five-axis projection is therefore activity satisfied, alignment satisfied,
predicate recovery satisfied, preservation not satisfied, and stable hold not
observed. Its Core diagnostic authority fields remain false; the actual approval
and dispatch receipts stay separate runtime evidence.

This is a negative registered-skill result, not a basis for a selector or a
second Repair strategy. The first preservation violation remains the terminal
outcome of this exercise; completion is false.

See
[`evidence/20260831-libero-registered-skill-same-world-repair.json`](evidence/20260831-libero-registered-skill-same-world-repair.json).

## Reproduction

Fixture generation and execution are opt-in because they start a real LIBERO
simulator process:

```bash
RUN_MISSIONOS_LIBERO_REGISTERED_SKILL_FIXTURE=1 \
python scripts/generate_libero_registered_skill_fixture.py \
  --output-dir /tmp/registered-skill-fixture

RUN_MISSIONOS_LIBERO_REGISTERED_SKILL_REPAIR=1 \
python scripts/run_libero_registered_skill_same_world_repair.py \
  --snapshot /tmp/registered-skill-fixture/fixture.npz \
  --output /tmp/registered-skill-repair.json \
  --dispatch-state /tmp/registered-skill-dispatch.json \
  --operator-approval-ref <real-operator-approval-reference> \
  --maximum-repair-steps 128
```

The runner refuses an existing output or dispatch ledger. A retry requires new
output paths and a new approval-bound dispatch; it never overwrites evidence.
