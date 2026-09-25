# Try depth-based navigation in PX4 simulation

MissionOS can use a camera's depth measurements to choose a route through a
building gap, over a low building, or around a tall obstacle. These are three
fixed simulator profiles. The selector chooses from supplied routes; it does
not generate a route through an arbitrary city.

With the [simulator configuration](../agents/px4-depth-gateway.md) enabled on the
Gateway, prepare a task, approve its execution, and inspect the result:

```sh
missionos prepare-px4-depth --scene climb
missionos execute-sitl --task-id TASK_ID
missionos job-status --task-id TASK_ID
```

Use `gap`, `climb` or `detour` for the scene. Preparation does not fly. The
`execute-sitl` command records your approval for choosing one of that task's
declared routes, followed by landing. Depth observations inform the choice;
independent constraints can reject it. Completion requires observed arrival,
landing and disarm, followed by verification.

The task records the selected route and outcome. These runs use no WAM, Jev or
GPU and do not release a payload. A failed task retains its evidence and cannot
be silently retried with the same approval.

For a planned stop and one route change, use the gap profile:

```sh
missionos prepare-px4-depth --scene gap --reobserve
missionos execute-sitl --task-id TASK_ID
missionos job-status --task-id TASK_ID
```

Here, a static obstacle appears after departure. The aircraft stops at a supplied
checkpoint, measures depth again and chooses from the declared resume routes.
The execution approval covers this single checkpoint and at most one resume
decision. The same task retains the initial choice, stop, new choice, observed
dispatch and verified outcome. If no route is selected or the constraints reject it,
landing is recorded as a safe abort, without claiming destination arrival.

This option is limited to this simulator scenario. It does not provide continuous
emergency avoidance or route generation for arbitrary missions.
