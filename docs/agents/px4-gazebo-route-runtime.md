# PX4/Gazebo Route Runtime

This document describes the maintained horizontal-route runtime boundary and
the contracts that must survive future refactors.

The opt-in entrypoint remains:

```text
scripts/smoke_px4_gazebo_horizontal_route_delivery.py
```

Despite the historical filename, this program is a production runtime harness,
not a disposable logic-only smoke. It delegates bounded responsibilities to
`src/runtime/px4_gazebo_route/`.

## Authority Boundary

Preserve the MissionOS split:

```text
LLM judges.
Human approves.
Rules constrain.
Executor acts.
Verifier checks.
Repair loops.
```

The extracted modules must not collapse these facts:

```text
proposal
operator approval
allowlist binding
dispatch attempt
transport ACK
runtime state observation
behavior delta
recovery completion
delivery completion
physical execution
```

In particular:

- observation and reporting code cannot mint approval or dispatch authority
- route bootstrap requires supplied, fresh operator approval
- an upload or command ACK is not motion, waypoint arrival, landing, recovery,
  or delivery completion
- a second recovery action requires its own approval and dispatch references
- terminal-action orchestration receives authority callbacks explicitly
- alternate-route progress does not prove the original dropoff
- visual markers do not become geofence enforcement or route blocking
- simulator execution remains opt-in and does not claim physical execution

## Module Ownership

The package keeps environment-specific mechanics visible rather than hiding
them behind a single coordinator.

| Area | Modules |
| --- | --- |
| Configuration and environment | `configuration`, `environment`, `world`, `scenario`, `environmental_realism` |
| Transport and execution | `embedded_mavlink`, `execution`, `alternate_route` |
| Observation and verification | `observation`, `verification`, `collision_observation`, `contact_integration`, `dynamic_observation` |
| Recovery | `recovery_execution`, `recovery_outcomes`, `recovery_workflow`, `recovery_persistence`, `recovery_reporting` |
| Operational decisions | `operational_verification`, `operational_outcomes`, `route_decision`, `terminal_action` |
| Persistence and audit | `artifacts`, `audit`, `reporting`, `finalization`, `bootstrap` |
| Supervision | `supervision` |

Import concrete modules directly. Do not introduce a facade that hides which
component can dispatch, observe, verify, or persist.

## Smoke Classification

Classify a `smoke_*.py` program before removing it:

```text
artifact_contract
opt_in_runtime_smoke
production_runtime_harness
historical_one_off
unknown
```

An `artifact_contract` may move to pytest only after a maintained fixture covers
its scenario, expected status, artifact bindings, and false authority claims.
Keep opt-in runtime smokes and production runtime harnesses until an equivalent
runtime boundary has been exercised.

The contract migrations in this slice replace one-off scripts with fixtures and
focused tests. Test success does not claim live PX4/Gazebo behavior.

## Verification

Run the focused package contracts:

```bash
.venv/bin/python -m pytest -q tests/contract/test_px4_gazebo_route_*.py
```

Run the full public suite before publication:

```bash
.venv/bin/python -m pytest -q
```

Exercise the real entrypoint with execution authority absent:

```bash
env -u RUN_PX4_GAZEBO_HORIZONTAL_ROUTE_SMOKE \
  .venv/bin/python scripts/smoke_px4_gazebo_horizontal_route_delivery.py
```

The command must exit closed before PX4 or Gazebo starts and identify the
missing opt-in gate.

Changes to simulator behavior, process lifecycle, transport, or observation
require an opt-in PX4/Gazebo runtime check in addition to tests. Record the
exact command, scenario, observed result, and limitations in the PR.

## Refactor Rule

Refactoring may move code but must not strengthen evidence. Preserve supplied
facts and keep early terminal returns visible. Every transport call must retain
its explicit approval and allowlist binding, and no extracted observer may
create either one.
