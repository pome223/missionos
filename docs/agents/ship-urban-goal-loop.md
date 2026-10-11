# Bounded decisions toward an observed fixture goal

Status: local CPU fixture integration. This extends the existing urban loop
with an approved goal, current obstacle observations and goal-based termination.
It does not connect AeroVLA, ANWM, PX4 or Gazebo, change the native Yokohama
checkpoint flow, or demonstrate learned model quality, flight or delivery.

## Approval and limits

`UrbanLoopPlan.fixture_goal_policy=True` requires fixture execution scope, an
explicit `goal_ned_m` inside the approved corridor, at most three updates,
segments no longer than 5 m and clearance of at least 0.25 m. The goal must
differ from the entry by more than the arrival tolerance. The existing clock,
reserve, image freshness, hold, single-use permit and process ownership limits
still apply. There are no inference retries: the maximum is six requests and
three dispatches. A goal reached earlier ends the loop earlier.

Each request binds the same approved goal, tolerance, segment limit and
clearance to its plan hash. Models cannot replace the goal or widen the
corridor. The VLA role proposes one to four candidates; the WAM role provides
an explicit forecast for each exact candidate and selects one. Neither role
grants execution authority. Independent Rules authorize the chosen segment;
the AP checks and consumes the expiring permit.

## Current observations and failure behavior

Goal-mode observations contain only the sensor fields and `goal_context`.
The latter has schema `ship_urban_observed_geometry.v1`, a fresh observation
time, corridor-wide coverage and up to 32 observed axis-aligned obstacle boxes.
Unknown fields, future timestamps and incomplete coverage reject. Obstacle
geometry is synthetic and fully observed; this is not a sensor or perception
implementation. Unknown space cannot be treated as clear.

The context hash covers geometry and coverage, sorts obstacle IDs and excludes
the refreshed timestamp. The WAM response binds that hash and the exact VLA
proposal, with one candidate hash and boolean `predicted_clear` per candidate
under `ship_urban_goal_forecast.fixture.v1`. A missing, malformed, timed-out,
unbound or selected-blocked forecast stops the loop. Rules independently check
the swept segment against expanded boxes. Touching a clearance boundary blocks.

The runtime checks context on every observation while either request is pending,
including transient changes that later revert. The runtime and fixture AP check
it again before dispatch. Every recorded observation during a segment must
preserve that context and clearance; a change stops execution in fixture hold.
Between completed segments the next
fresh observation may introduce new geometry, causing a new decision. This
does not qualify avoidance of an obstacle appearing during physical flight.

Completion requires stable AP arrival within the approved goal tolerance,
recorded as `goal_arrival_observed`, followed by held exit, session revocation,
verified worker shutdown and fixture return handoff. Exhausting the update
budget without observed goal arrival produces a blocked result. Failure never
expands authority or counts as successful arrival.

## Reproducible process and HTTP smoke

Run each scenario with a new output directory:

```sh
PYTHONPATH=packages/missionos-core/src:packages/missionos-cli/src:. \
  python -c 'from missionos_cli.cli import missionos; missionos()' \
  ship-delivery urban-loop-smoke --approve-fixture --goal-responsive \
  --obstacle-side left --output-dir /tmp/new-observed-goal-left
```

Repeat with `--obstacle-side right` and `none`. Each successful scenario uses
two actual local Python processes and six loopback HTTP inference requests.
Both roles are deterministic CPU doubles. They receive current observations,
the approved goal and the bound proposal; they receive no scenario side,
future obstacle schedule or expected selection.

All scenarios start at `[1015,0,-30]`, share a first arrival at `[1019,0,-30]`
and aim at `[1027,0,-30]`. Only after the first arrival does the fixture world
introduce the left or right box. The next observation changes the second target
to `[1023,1,-30]` or `[1023,-1,-30]`, respectively. The third segment reaches
the goal. The no-obstacle case retains the straight path. Candidate generation
uses current position and goal; selection uses current geometry and remaining
goal distance, not the cycle number.

The saved `result.json` and `verification.json` remain separate. The verifier
reconstructs request, response, candidate, permit, observation and shutdown
bindings, derives action counts from events, and uses its own slab intersection
implementation to check geometry and observed motion. It requires observed goal
arrival rather than trusting `goal_reached` or `completed_updates`. Its native
model, physical execution, whole-mission and energy claims remain false.

Contract tests include mirrored observations, early goal termination, budget
exhaustion, forecast/context failures, hidden future fields and tampered
completion receipts. Existing default-mode receipts keep their legacy verifier
path. No cloud resource, paid inference or native trial is needed for these
checks.
