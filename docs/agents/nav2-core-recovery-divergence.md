# Nav2 Core Recovery Candidate Divergence

This note resolves the publication-safe part of public issue #66. It compares a
blocked Recovery candidate fixture with a verified reference fixture through the
same production Core adapter.

## Finding

The public fixture is blocked because it does not contain a source-backed
obstacle height, `runtime_obstacle_size_z_m`. The policy binding, costmap
snapshots, candidate path, and robot collision envelope are otherwise the same
as the verified reference. Core therefore cannot prove 3D clearance and records:

- candidate status `unverified`;
- `nav2_obstacle_geometry_unverified`;
- `nav2_candidate_3d_clearance_unverified`;
- overall `no_core_verified_recovery_candidate`.

This is a missing-evidence result, not a policy relaxation opportunity. The
candidate remains unselectable.

## Comparison boundary

The historical internal route artifact is intentionally not published and is
not an input to this comparison. Consequently, this repository proves the
failure class and the exact public fixture difference; it does not claim that
the historical private route had the same values.

The report includes, for every candidate:

- Core status and blocked or unverified reasons;
- verification items and their basis;
- evidence references;
- observed-fact source provenance;
- policy binding;
- authority flags fixed to false.

An internal candidate selection, a public fixture proposal, or a Core verdict
does not prove human approval, dispatch, observed motion, completion, or
physical execution.

## Runtime verification

Run the production adapter against both committed public fixtures:

```bash
PYTHONPATH=packages/missionos-core/src:packages/missionos-cli/src:. \
  .venv/bin/python scripts/smoke_nav2_core_recovery_divergence.py
```

The command succeeds only when the blocked fixture remains fail-closed, the
verified fixture remains feasible under the same policy binding, the missing
input is exactly `runtime_obstacle_size_z_m`, and all authority flags remain
false.
