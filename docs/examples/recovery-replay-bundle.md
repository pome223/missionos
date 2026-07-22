# PX4 Recovery Replay Bundle

This example builds a sanitized replay from a deterministic synthetic task. It
contains two separate Recovery epochs and four bounded local-NED telemetry
samples. The fixture intentionally contains fields that must never appear in a
public bundle, so the smoke also exercises the publication boundary.

Generate the synthetic source, export it, and verify the result:

```bash
tmp_dir="$(mktemp -d)"
python tests/fixtures/px4_recovery_replay.py \
  --output "$tmp_dir/source-task.json"
python scripts/missionos_recovery_replay_bundle.py export \
  --task-json "$tmp_dir/source-task.json" \
  --public-run-ref fixture-two-recovery-cycles \
  --output "$tmp_dir/replay-bundle.json"
python scripts/missionos_recovery_replay_bundle.py verify \
  --bundle "$tmp_dir/replay-bundle.json"
```

Observed verifier result:

```text
verification_status=verified
closed_loop_cycle_count=2
causal_form=Form 3
blocking_reasons=[]
delivery_completion_claimed=false
physical_execution_invoked=false
```

This is a deterministic contract smoke. It does not prove that a simulator ran,
that an executor acted, or that a physical delivery occurred. A bundle exported
from a real private task must pass the same sanitizer and verifier before it is
considered for publication, and the source evidence remains subject to a
separate review.
