# CLI → Gateway → depth selector → PX4 integration

These are three new simulator profile checks plus one post-fix climb regression through the normal CLI,
TaskStore and SITL approval/execute endpoints. They are not extra samples in
the frozen twelve-case R2 experiment or a WAM efficacy comparison.

`summary.json` is a reviewed export of the verified task results. It excludes
task IDs, approval IDs, credentials, local paths and raw observations.
`manifest.json` binds its bytes; `verify_report.py` checks the scope and terminal
claims. Raw flight records were independently verified before this export.
These public checks do not replay physics or substitute for the retained raw
records.

See the [execution contract and commands](../../agents/px4-depth-gateway.md).

```sh
python docs/assets/px4-depth-gateway-20260924/verify_report.py
```

The first three task results retained the underlying verifier schema name due to a merge-order defect. Their flight facts are preserved unchanged. The correction keeps that source schema separately; the fresh climb regression uses `px4_depth_navigation_result.v1`. This is an integration repair, not an additional efficacy cohort.
