# LIBERO scripted Repair failure fixtures

This diagnostic path creates an explicit failed simulator state before the
normal MissionOS Repair authority path begins. It exists to avoid treating a
millimetre-scale predicate-boundary miss as a representative VLA recovery
problem.

## Authority boundary

Fixture injection is test setup only:

```text
script creates failed state
  -> Verifier confirms [true, false, true]
  -> Human-approved Repair proposal is created
  -> governed dispatch is consumed once
  -> executor applies VLA actions
  -> Verifier checks preserve and target predicates
```

The injection step creates no proposal, approval, dispatch, controller ACK, or
physical execution claim. A successful run is reported as
`scripted_fixture_repair_established`; it is not counted as a naturally
occurring Repair success and does not estimate a general recovery rate.

## Frozen scenarios

The runtime accepts four named fixtures. Their distances, settling budgets, and
validation gates are code-frozen in
`src/runtime/libero_repair_failure_fixture.py`.

- `displaced_from_stove`: the second pot is upright and clearly outside the
  stove region.
- `wrong_table_location`: the second pot is placed farther away at a distinct
  table location.
- `tipped_over`: the second pot is moved outside the stove region and rotated
  by 90 degrees.
- `dropped_during_scripted_transfer`: the second pot is released from a raised
  transfer pose and allowed to settle. This is a scripted release fixture, not
  evidence that a policy previously grasped and dropped the object.

Every fixture fails closed unless all of the following are observed before the
Repair proposal is created:

- terminal goal vector is exactly `[true, false, true]`;
- the protected first pot and stove-on predicate remain true;
- the target moved by at least five centimetres;
- the target is at least five centimetres outside the stove boundary;
- the object has settled below the scenario's velocity limits;
- the protected pot moved no more than five millimetres;
- tipped and dropped fixtures satisfy their additional orientation or fall
  gates.

## Opt-in invocation

The production runner remains opt-in and requires an operator approval
reference plus a single-use dispatch ledger. For example:

```bash
RUN_MISSIONOS_GROOT_LEROBOT_SAME_WORLD_REPAIR=1 \
python scripts/run_groot_lerobot_same_world_repair.py \
  --runtime live \
  --checkpoint-path /opt/model-cache/POLICY_SNAPSHOT \
  --operator-approval-ref OPERATOR_APPROVAL_REF \
  --dispatch-state-path /tmp/missionos-dispatch-ledger.json \
  --output /tmp/missionos-scripted-fixture-result.json \
  --source-failure-basis scripted_failure_fixture \
  --scripted-failure-fixture displaced_from_stove \
  --maximum-repair-chunks 45
```

Replace the placeholder checkpoint and approval values at run time. Do not
commit live evidence, credentials, private paths, or dispatch state.

## Required measurement order

Run the scenarios from least to most demanding:

1. displaced from stove;
2. wrong table location;
3. tipped over;
4. dropped during scripted transfer.

Before using a scenario as VLA capability evidence, independently demonstrate
recoverability through the same 7D simulator action interface and within the
same action budget. If recoverability is not established, report the scenario
as `unmeasured_as_repair_capability`; do not interpret a VLA failure.
