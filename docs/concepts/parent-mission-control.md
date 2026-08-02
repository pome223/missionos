# Parent mission control

MissionOS can place several already-approved executor stages under one parent
mission without pretending that their individual results prove the whole
mission.

The parent freezes:

- the order of the stages
- the executor and child contract used by each stage
- the predicate package allowed to evaluate each child result
- the approved target description and the operator approval that cover the plan

A completed stage is only a prerequisite for the next stage. It does not create
permission by itself. The next stage is allowed only when the original parent
approval already covered it.

```text
parent approval
  ├─ stage 1 contract → stage 1 result
  ├─ stage 2 contract → stage 2 result
  └─ stage 3 contract → stage 3 result

stage 1 result + existing parent approval → stage 2 may start
stage 2 result + existing parent approval → stage 3 may start
```

Even when every stage predicate is satisfied, MissionOS still does not say
"mission completed" unless a separate, approved parent-level completion rule
exists. A shared description also does not prove that separate simulators saw
the same physical object or even the same world.

The first implementation proves authority and evidence lineage. It does not
prove physical identity, physical execution, or a shared-world outcome.

The first live three-stage example sequenced PX4/Gazebo,
Nav2/TurtleBot3, and GR00T/LIBERO Panda under one parent run identity. Each
stage met its own bounded simulator predicate. MissionOS still reported parent
completion as unverified because the three simulators did not share one world
or one physical target.

## Natural-language task selection

Natural language may select an approved VLA task, but it cannot create a new
robot task. MissionOS shows the exact catalog task and instruction before the
operator approves it. If a concrete request has no approved match, MissionOS
stops before creating a proposal rather than running the closest available
task.

The first catalog contains one GR00T/LIBERO Panda simulator task: turn on the
stove and place the moka pot on it. This is an environment-bound task; it does
not mean that arbitrary chat text was sent directly to the model.

The pinned `libero_10` model suite contains nine additional source-backed task
candidates. MissionOS shows them as unverified candidates rather than silently
promoting them into executable catalog entries. Arbitrary requests such as
assembling a cardboard box or driving a screw are accepted as unmet capability
requests, not converted into robot skills by changing the prompt.

Chat can still help author the contract. For a known source-backed task it
shows a success-condition draft derived from the pinned environment definition.
For an unknown task it shows an unverified draft and the missing observations
or training work. In both cases, drafting a condition is separate from
approving it or claiming that the condition was satisfied.

## Operator view

The operator entry point is `missionos chat`. An operator request can select
either the exact GR00T/LIBERO Panda simulator package or the approved PX4 →
Nav2 → GR00T package. Chat shows the proposal and still requires separate
`/approve` and `/run` commands. Natural language cannot invent the predicate,
reorder stages, or grant execution authority.

One live three-stage run initiated from `missionos chat` completed all three
bounded simulator stages under the same parent run identity. That run did not
prove parent completion, a shared simulator world, arbitrary language delivery
to GR00T, or physical execution.

Once started, the task ID returned by chat is the same task read by:

```bash
missionos job-status --task-id <task-id>
```

For a stored parent task, `missionos job-status` groups the cross-layer facts
an operator needs to read together: current stage if recorded,
Virtual-to-Real promotion status, safe stop, repair, operator intervention,
operational closure, and physical execution.

The view does not fill gaps. A missing record is shown as `unknown`, while a
recorded negative remains `False`. A valid promotion receipt is still only a
prerequisite; it does not by itself authorize a physical deployment or prove
that the target is safe. Evidence from separate runs is never combined into
one apparent mission.

## Post-episode VLA repair

The official LIBERO runner does not expose an external stop or recovery
callback while an episode is running. MissionOS therefore cannot intervene in
that episode.

After a failed episode ends, MissionOS can offer one bounded repair action:
retry the same approved catalog task in a new episode. The proposal binds the
failed task, its approval, and its failure evidence. It does nothing until a
human approves it. Approval creates a new run identity, episode identity,
contract, and task; it never rewrites the failed run.

This first repair path does not change the task, initial pose, controller, or
success predicate. It allows one retry only. A second failure remains failed
and creates no further retry proposal. Simulator retry also does not prove an
in-episode stop, controller ACK, safe-stop effect, or physical execution.
