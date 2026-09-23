# Aerial WAM value experiment: choose a useful inspection view

Status: **experiment design, not an observed WAM benefit**. The paired PX4
calibration flight established an integration path and an unfavorable prediction
result. The next objective is to obtain a usable target inspection image with
fewer failed viewpoints and repeat visits. This gives a future observation a
task consequence; full-frame RGB similarity alone does not define that outcome.

## Why the calibration scene is insufficient

The CPU audit in `scripts/audit_px4_anwm_value.py` revalidates the paired flight,
input and forecast artifacts through `compare_px4_anwm_outcomes.py`. It uses the
published scene geometry **only for retrospective auditing**. It emits aggregate
counts and costs; it cannot admit a prediction or dispatch a flight.

In both measured histories, the complete green target box lies outside the left
plane of the raw 640 × 360 camera frustum in all sixteen frames. The scene-specific
color diagnostic also counts zero green pixels in all history frames and all
four predicted images, while the goal contains 49,344 green pixels. Its rule is
`G > 2R`, `G > 2B`, `G > 0.1` in RGB scaled to [0, 1]. This is an artificial-marker
diagnostic, not a general detector or proof about indirect lighting cues.

Therefore, the current input supplies no direct view of the distinguishing
target. Training a selector on a fixed "green is left" layout could exploit that
layout without learning useful prediction. Color/layout swaps and unseen scenes
are necessary controls. Completely unobserved, independently randomized target
identity is an information-acquisition case, not a fair demand to guess it.

The available goal-pose baseline already selects the better measured arm. Its
paired image-MSE regret is zero, versus 0.014968 for ANWM and projection MSE.
This is a two-flight proxy with near-matched starts, not a complete same-state
outcome matrix or a mission success rate. It supplies no improvement headroom
over the strongest available baseline in this particular scene.

The recorded inference takes 41.18–41.22 wall seconds for two candidates. The
candidate motion takes about 6.8 **simulation** seconds; those clocks must not be
treated as measured end-to-end time savings. A useful low-rate inspection planner
must recover its inference cost through avoided travel or failed observations.
This implementation has not established an emergency collision-avoidance loop.

## Bounded hypothesis

For a static inspection target with an image reference and a prior partial view,
ANWM's action-conditioned future observation may rank candidate viewpoints better
than projecting the same measured RGB-D history. The intended benefit is a usable
first inspection view or fewer repeat visits. This hypothesis concerns viewpoint
selection; generated RGB is not obstacle clearance or landing evidence.

The specific opportunity is a viewpoint that reveals a partly observed facade
or target surface: raw reprojection leaves missing appearance, while a learned
model might preserve task-relevant identity and shape there. Use textured scenes
and actual sensor limitations. Do not artificially remove good depth or history
from a comparator to manufacture this opportunity. Fully observed static geometry
is an essential control and may leave no useful job for the learned model.

Use an image-reference mission where the target's global location is genuinely
unavailable. Do not hide an available target pose merely to weaken the baseline.
When the mission provides a target pose, retain the pose-based planner. Rules
still receive the geofence and authoritative obstacle information they need.
Future rendered images and target masks are verifier labels, never predictor
inputs. Every selector receives the same observation history and candidate set.

Candidate families are a short observation maneuver, two inspection viewpoints,
and hold/abstain. These are proposed experimental actions; the existing public
flight contract still permits only its declared fixed candidates. New maneuvers
need their own bounded execution contract and actual motion verification before
they can enter a flight experiment.

## CPU and simulator screen before another GPU run

1. Build twelve development cases from several static layouts, with mirrored
   targets, changed backgrounds, partial occlusion and varied viewing angles.
   Require a distinguishing target cue in the supplied history, documented before
   learned inference. Add fully observed and unobserved-target controls separately;
   do not count information-free guessing failures toward recoverable headroom.
   Use held-out layouts and target appearances later; mirroring a development
   image is not an independent test case.
2. Capture real simulator observations from one restored state for every candidate.
   Record state restoration error and obtain an outcome matrix. Static camera
   renders can screen visibility cheaply, but do not count as feasible drone
   motion or completed flight. Later, confirm retained candidates through PX4.
3. Define a usable inspection image before scoring: the correct target has at
   least 80% unoccluded projected area and at least 64 pixels on its shorter side
   in the raw 640 × 360 image. Simulator target masks measure these labels; neither
   mask nor outcome image is exposed to the selector. Freeze any application-driven
   change to these thresholds before the test set is captured.
4. Measure the following fixed comparators: development-selected constant action;
   current-image target matching plus geometric planning; all-history RGB-D
   reprojection plus target matching; a geometric visibility/information-gain
   observation policy; and goal-pose planning whenever available. Count sensing
   maneuvers and repeat visits for every policy. Use the same frozen task scorer
   on projection and learned future images to isolate the learned contribution.
5. Continue to GPU only if a feasible candidate succeeds in at least ten of the
   twelve development cases, and the strongest applicable cheap baseline misses
   at least three of those recoverable cases. These are prespecified screening
   thresholds, not statistical evidence. If target information is missing, first
   test acquiring it. If geometry solves the task, use geometry and change the
   research question instead of weakening it.

The color audit above does not implement the new inspection scorer or this
outcome matrix. No new scene, flight, inference or headroom result is claimed.

## Freeze the comparison, then test the contribution

After passing the screen, freeze the scorer and policies on the development set
and collect twenty-four held-out cases using the same observability eligibility
rule; report the extra controls separately. Keep the set size and decision rule fixed
before inspecting learned-model results. Report each candidate's observed outcome,
the hindsight best candidate, every method's selection and abstention, and failures.

The primary endpoint is the usable first-view rate at the same allowed decision
deadline. A pilot continuation criterion is at least three additional usable
first views out of twenty-four over the strongest applicable cheap baseline;
report paired uncertainty and do not call this small pilot statistical proof.
Also report wasted travel, repeat views, inference wall time and per-case compute
cost. A method that misses the deadline counts as a failure, not an excluded case.
Set that deadline from the operational inspection budget before test collection.
Do not sum slowed-simulator motion time and GPU wall time into a claimed real-world
completion time; validate the timing relationship or report the components.

Use the following ablations to explain any gain:

| Comparison | What it can establish |
| --- | --- |
| Projection + task scorer vs projection + RGB MSE | Scorer benefit, not WAM benefit |
| ANWM + task scorer vs projection + the same scorer | Increment from learned future imagery |
| ANWM vs the strongest history/geometry observation policy | Benefit beyond a cheap planner |
| Both methods with an extra observation | Value of sensing separately from model inference |
| Held-out outcomes vs development outcomes | Whether the result survives layout/appearance changes |

Post-training becomes justified only when the task has recoverable headroom,
information sufficient to distinguish useful actions, and a reproducible model
failure on that distinction. Fine-tuning must use separate training captures;
the held-out matrix cannot become training data while retaining its test label.
Recheck terminal outcomes and inference cost after training, not just image loss.

Jev may judge admitted, bounded evidence; it does not turn a forecast into an
approval or a calibrated risk. Human/policy authorization, Rules, Executor and
Verifier retain their existing separate roles. The WAM may also abstain: making
an unsupported image look plausible is not task value.

## Reproduce the completed audit

With the two local captures from technical report section 11:

```sh
PYTHONPATH=.:packages/missionos-core/src python scripts/audit_px4_anwm_value.py \
  --left-root "$LEFT_CAPTURE_ROOT" --right-root "$RIGHT_CAPTURE_ROOT" \
  --output "$LOCAL_VALUE_AUDIT_JSON"
python -m pytest -q tests/contract/test_px4_anwm_value_audit.py \
  tests/contract/test_px4_anwm_outcome_images.py
```

The CLI validates existing real capture and inference artifacts and computes the
CPU audit. It performs no fresh GPU inference, Jev call, flight or change to the
production selector. Raw images, authorization records and cloud identifiers
remain outside the public report.
