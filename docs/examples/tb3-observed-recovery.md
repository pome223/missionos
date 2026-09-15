# TB3: recover when waiting does not clear the route

A TurtleBot3 must reach a fixed goal while a wide red obstacle crosses its
route. A four-second learned prediction can justify waiting. The robot then
checks a real simulator camera image: if the route remains blocked, Recovery
can propose a separately approved detour. The proposal passes Assurance and
Rules before Nav2 receives the fixed goals. Arrival is checked against observed
robot motion and position.

The useful change is **recovering from a failed wait and reaching the goal**.
Waiting is no longer the only authorized response to this scene. A second
path handles a prediction that expires during Agent processing: reobserve,
then consider a separately approved continuation if the route is actually clear.

| Role | Responsibility |
| --- | --- |
| Human | Approve the bounded wait, optional detour, and optional original-route continuation separately |
| Assurance | Judge the mission obstruction and review Recovery's proposal |
| Recovery | Select an action using prediction or fresh observed evidence |
| Rules | Enforce approval, evidence freshness, fixed goal bounds, and one-use budgets |
| Executor | Submit the authorized fixed Nav2 goals |
| Verifier | Check actual clearance, native goal results, observed position, and cleanup |

An Agent answer alone never dispatches a goal. If the required approval is
absent, the run stops before that action. This example uses the existing
Assurance graph and PolicyStore through a dedicated opt-in CLI; it does not
exercise Gateway chat or physical hardware.

## Try the model-free comparison

After the [environment setup](../agents/tb3-predictive-navigation.md#setup),
preview the fixed image-history policy from the repository root:

```sh
missionos navigation run tb3 --policy image-history \
  --config examples/navigation/tb3-history.json \
  --output output/tb3-history
```

Add `--run-sim` to authorize the local simulator run. Use a new output directory
for every trial, then inspect it with `missionos navigation status output/tb3-history`.
This comparison uses no learned model or external Agent call. The Agent path
requires the separately supplied assets described in the maintainer guide.

## What was observed

The [public-source trial record](../agents/tb3-predictive-navigation.md#public-source-verification)
includes both arrival and intentional stops. In the slower-obstacle condition,
wait-only authority stopped with zero goal requests; separately approved
Recovery used the failed-wait observation, proposed the fixed detour, and
reached the goal. With an injected processing delay, missing continuation
authority also stopped with zero goals; granting that distinct authority
allowed a fresh-observation continuation to reach the goal.

The original wait, actual-clearance, Assurance-continuation path was also
exercised on the public source. Time is reported as a secondary measurement;
beating the existing rules policy is not the acceptance criterion.

## Limits

This is a small, known Gazebo/Nav2 scene with a synthetic red-obstacle mask,
fixed goals, a legacy controller, and an opt-in incoming heading at the detour
point. Intermediate and final observed positions use an existing 0.30 m bound;
this does not prove exact passage through a narrow waypoint. Route planning
and low image exposure do not establish collision freedom.

The optional predictor uses a task-adapted NWM future-mask head. Its weights,
training data, and upstream model implementation are not included. The result
is not a stock-model benchmark, a general navigation qualification, a PX4
result, or evidence of physical operation. See the
[contracts, commands, results, and asset requirements](../agents/tb3-predictive-navigation.md).
