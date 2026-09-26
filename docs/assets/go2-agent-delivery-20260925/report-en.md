# MissionOS × Go2: From Agent Supervision to Verified Delivery

**Validation date: September 25, 2026 | Code revision: `cf3db6c` | Indoor MuJoCo simulation**

[日本語版 / Japanese version](index.html)

MissionOS Go2 chat now defaults to Agent supervision. The verified workflow connects a delivery request, human approval, execution, simulated receipt, return, and terminal verification. During a temporary passage closure, actual DeepSeek responses selected “wait for 10 seconds” and “replan the route.” Both decisions passed Rules checks, and the executor resumed navigation. In a separate moving-obstacle run, the robot's local controller handled yielding and resumption without calling the LLM.

This report demonstrates **an operational connection between judgment, approval, constraints, execution, and verification**. It does not demonstrate physical delivery, autonomous camera perception, WAM integration, or a performance improvement over simple rules.

## 1. Two Videos, Two Control Responsibilities

### A. Yielding to a Moving Obstacle — The Requested Video

This is the recording linked from the browser's Go2 delivery page. The default supervision setting was “MissionOS Agent judges the situation,” and delivery completed at 87.390 simulation seconds. However, this run did not require escalation to mission-level exception handling: **the LLM made zero judgments**. The video's `host Agent enabled` label indicates configuration, not evidence that inference occurred.

[Video A: Yielding, delivery, and return](media/moving.mp4)

<!-- VIDEO_MOVING -->

The local controller responds to the red-orange obstacle by requesting a stop, confirming the stopped state from measured speed, checking the path again, and resuming. The lower-level walking policy continues to generate joint targets and maintain posture. A human did not control individual footsteps or stop timings in this recording.

### B. Waiting for a Temporary Closure — Actual Agent Judgments

This is a separate run initiated through the CLI. The first outbound navigation attempt was interrupted by a passage closure. Two Agent judgments then led to resumed delivery. Simulated receipt, return, and terminal holding were verified at 95.455 simulation seconds.

[Video B: Temporary closure, Agent-directed waiting and replanning, delivery, and return](media/agent.mp4)

<!-- VIDEO_AGENT -->

Both videos render actual MuJoCo physics states; they are not generated predictions of future scenes. Each recording is 1280 × 800 pixels at 30 fps, with approximately 3× playback speed. Video A lasts 29.167 seconds and Video B 31.867 seconds. These playback durations are neither simulation elapsed time nor wall-clock duration. The inset map uses known geometry and pose readback from the simulator. Video duration must not be used to estimate inference latency or real-world delivery time.

## 2. Measured Results and Trial Scope

This detailed report covers **two saved runs: one run in each of two different scenarios**. Both were configured for Agent supervision, but actual Agent invocation was checked separately in the results. Run A was started through the browser; Run B was started by the CLI verification workflow. This is not a matched Agent-on/Agent-off comparison. Two completed runs out of these two examples do not establish a general success rate. Nor is this report a benchmark covering the entire development history and all failures.

| Metric | A: Moving obstacle | B: Temporary passage closure |
| --- | --- | --- |
| Scenario | `moving_obstacle` | `temporary_blockage` |
| Supervision configuration | Agent | Agent |
| Actual LLM judgments | 0 | 2: `wait` → `reroute` |
| Terminal status | `completed` | `completed` |
| Terminal simulation timestamp | 87.390 s | 95.455 s |
| Outbound arrival verified at | 44.760 s | 53.130 s |
| Return arrival verified at | 82.365 s | 90.430 s |
| Terminal hold | 5.0 s | 5.0 s |
| Final distance from reception center | 0.157940 m | 0.160051 m |
| Final measured speed | 0.001043 m/s | 0.001375 m/s |
| Local yield requests | 2 | 0; no moving obstacle |
| Physics steps with moving-obstacle contact | 0 | Not applicable; feature disabled |
| Minimum conservative clearance | 0.546451 m | Not applicable |
| Physics steps | 17,478 | 19,091 |
| Walking-policy inference calls | 4,370 | 4,773 |
| Saved telemetry samples | 874 | 955 |

For both runs, the terminal contact list was empty, the latched safety-violation flag was false, and simulated receipt was true. Run B preserves its first outbound attempt as `verified=false`; that attempt was not replaced with a successful one. The interruption was caused by the planned closure. Mission success is evaluated over the subsequent reobservation, judgment, and retry as well.

The difference between 87.390 and 95.455 seconds must not be interpreted as the cost or benefit of the Agent: the obstacle conditions differ. Each terminal timestamp measures elapsed simulation time from time zero, including preparation. It is not limited to time spent walking.

## 3. Interactive Replay of Recorded Observations

<!-- REPLAY -->

Select a scenario and move the time slider to inspect the recorded Go2 position, the traveled path, and either the moving obstacle or the passage closure. Replay uses simulation time and operates independently of the video players.

Coordinates are in the simulator's horizontal plane, in meters. Reception is at (−2.5, 0), and Meeting Room A is at (2.5, 0). The display uses the most recent observation at or before the selected time, without interpolation or prediction. All telemetry samples are retained; one terminal-state sample from the result file is appended. The resulting display datasets contain 875 and 956 points. Closure intervals come from Run B's own event timestamps, and moving-obstacle positions come from Run A's own observations.

This is a visualization of saved records. It is not proof of continuous collision-free motion, a localization evaluation, or a live physical-robot monitoring surface.

## 4. Architecture and Separation of Responsibilities

```text
Human: request delivery → approve the plan and action envelope
                                  ↓
MissionOS chat / browser → Gateway → Go2DeliveryMission
                                  ↓ interruption / exception observation
                         Host Agent (DeepSeek)
                                  ↓ proposed action and rationale
                         Rules: recheck fresh state and approved bounds
                                  ↓ admitted operation
                         Executor: wait / replan / return
                                  ↓
             Known-map A* + heading feedback + local velocity guard
                                  ↓ velocity command
                 RL-SAR walking policy → joint targets + PD → MuJoCo
                                  ↓ observations
                 Verifier → simulated receipt / return / terminal checks
```

**The Agent proposes an action; Rules constrain it; the Executor acts; the Verifier checks the outcome.** An Agent response is not itself approval or delivery completion.

- **Mission layer:** at most three exception judgments, a cumulative budget of 20 simulation seconds of explicitly requested waiting, replanning to the same destination, returning without delivery, or requesting operator attention. Ordinary chat input maps to supported operations; this is not unrestricted natural-language mission generation.
- **Navigation layer:** known-map A*, a 0.1 m grid, a 0.45 m clearance allowance, and heading feedback. Neither ROS 2 Nav2 nor Unitree's Sport API was invoked in these runs.
- **Local avoidance layer:** simulator-read obstacle positions and finite-difference velocity estimates support conflict detection, yielding, and resumption. This is not camera-based perception.
- **Locomotion layer:** an RL-SAR TorchScript walking policy runs on the CPU. It consumes IMU angular velocity, gravity direction, the velocity command, joint states, and the previous action to produce targets for 12 joints. Physics advances at 0.005 s per step, or 200 Hz. The policy updates every four physics steps, or 50 Hz. A torque-limited PD controller tracks the joint targets.

The Agent is `missionos_go2_supervisor_agent`, the model identifier is `deepseek-flash`, and invocation uses an ADK/LiteLLM standalone runner. This is not a VLA, an image-generating model, or a model that directly outputs joint actions. WAM components, including SimDist, NavThinker, and NWM, are not integrated into this execution path.

## 5. Video A: Local Avoidance in Detail

The scenario harness moves the obstacle. Navigation receives simulator position readback and velocity estimated from previous observations; it is not supplied with the future trajectory. Nevertheless, access to ground-truth position makes the task simpler than real camera- or LiDAR-based tracking.

| Event | Yield 1 | Yield 2 |
| --- | --- | --- |
| Yield requested | 3.215 s | 4.220 s |
| Stopped state observed | 3.685 s | 4.680 s |
| Request-to-stop-observation interval | 0.470 s | 0.460 s |
| Distance traveled after the stop request | 0.016884 m | 0.015025 m |
| Clear path observed | 4.035 s | 12.850 s |
| Yield duration | 0.820 s | 8.630 s |

The minimum center-to-center separation was 1.236451 m. Subtracting the conservative robot-envelope radius of 0.45 m and obstacle radius of 0.24 m yields 0.546451 m. This metric uses circular envelopes; it is not the measured distance between a foot and the actual obstacle surface. A second yield occurred shortly after the first resumption, so the behavior should not be described as one smooth, uninterrupted stop. That brief resumption and renewed stop should be included in any future assessment of the release condition.

## 6. Video B: Actual Agent Judgments and Their Execution Effect

| Simulation time | Observation, judgment, or execution |
| --- | --- |
| 9.005 s | The harness closes both upper and lower passages; outbound navigation is interrupted. |
| 11.005 s | After stopping, the state for `decision_1` is captured: no delivery route, a return route available, and a facility notice estimating reopening in about 10 seconds. |
| 16.215 s | The Agent's `wait(10.0)` passes Rules checks and waiting begins. |
| 21.005 s | The harness reopens the passages and updates the map revision. |
| 26.220 s | A fresh post-wait observation finds a delivery route. `decision_2` is requested. |
| 27.480 s | The Agent's `reroute` passes Rules checks; the route to the same approved destination is recomputed. |
| 53.130 s | Outbound arrival is verified with `source_decision_id=decision_2`. |
| 90.430 s | Following simulated receipt, arrival back at reception is verified. |
| 95.455 s | The 5-second terminal hold is verified and the mission completes. |

The first rationale was, in substance, “wait and reobserve, given the reopening estimate and remaining waiting budget.” The second was “the delivery route is now available, so replan to the same destination.” Verification checked request and response hashes, actual invocation evidence, Rules results, and the decision ID attached to executed navigation. This goes beyond observing a model response: the second judgment is linked to a verified outbound arrival.

The host-recorded wall-clock intervals from invocation start to completion were 5.259115 seconds for the first call and 1.090103 seconds for the second. These are observations from one run, not a general latency distribution or a measurement of generation time alone. They do not equal the simulator-side holding intervals, which also involve IPC waiting and fresh observation capture.

The entire 15.215-second interval from 11.005 to 26.220 seconds must not be described as a “10-second wait command.” The earlier portion includes holding posture while waiting for the Agent's response. The explicit 10-second wait begins at 16.215 seconds. Because a facility notice supplies the expected reopening time, this is not an experiment in which a WAM infers an unobserved future. Likewise, `reroute` means recomputing a route to the same destination; the identifier alone does not prove that a long alternative detour was selected.

## 7. Contracts for Approval, Rules, Stopping, and Arrival

### Approval and Agent Access

The CLI verification first confirmed that `/run` was rejected before approval, then issued `/approve` and `/run`. Approval is bound to the destination, scenario, input hashes, Agent configuration, and action envelope. Host credentials used by the Gateway are not passed to the simulator child process or browser. If the default Agent lacks connection configuration, the system fails explicitly rather than silently switching to fixed rules.

### Revalidating a Judgment

Rules check the request hash, observation ID, JSON schema, allowed action, finite waiting value and remaining budget, and rationale format. After the response, they also inspect fresh state: telemetry freshness, posture stability, geofence membership, heartbeat, cancellation, safety violations, contacts, displacement of no more than 0.25 m from the observed origin, and an unchanged map revision. Replanning additionally requires an available delivery route. A proposal is not executed merely because it was valid before the state changed.

The response-wait boundary is at most 35 wall-clock seconds per judgment. Physics and posture holding continue during this interval. The implementation does not wait indefinitely for an absent response.

### Arrival Settling and the Two Distance Tolerances

The PR's arrival correction preserves the movement executor's original 0.25 m goal tolerance. It begins stopping at an inner threshold of 0.7 times that tolerance, or 0.175 m, holds for two seconds, and reobserves. If the robot has drifted beyond 0.25 m, correction uses the original time budget. Cancellation or a safety violation such as contact during the hold prevents corrective motion.

Separately, the saved mission plan uses a 0.4 m distance tolerance for receipt and return-hold checks. **The executor's 0.25 m arrival threshold and the mission verifier's 0.4 m tolerance are distinct.** After returning, the mission checks state every second during a five-second hold, including safety violations, geofence membership, contacts, cancellation, and position bounds.

### Restarting After Abnormal Termination

At startup, the Gateway requests that interrupted deliveries stop. It blocks new dispatch until an inherited file lock establishes that the old worker process has exited. It does not rely on a PID check alone. Missing, replaced, or legacy evidence remains unverified. Recovery does not convert the interrupted mission into a successful one.

## 8. CLI, Browser, and Map Verification

| Surface or operation | What was checked |
| --- | --- |
| `missionos chat` | Go2 request, preapproval rejection, approval, execution, and terminal status. |
| Chat `/status` | Status of the current delivery task. |
| `job-status --task-id …` | The same task's result, configured Agent, observed responses, rationales, and Rules verdicts. |
| `operate` | Display while running and final result/judgment display. This verification does not cover arbitrary natural-language intervention. |
| `watch` | Updates during execution and terminal display. |
| `map` | The same task's indoor map, observed trajectory, completion status, and rationales. A static snapshot at generation time. |
| `missionos status` | Gateway health, separate from the delivery-status commands. |
| Browser | Agent selected by default, request-to-unapproved-plan flow, and Run A's completion display and recording link. |

Run B used the actual installed CLI, rather than substituting direct HTTP calls for CLI behavior. For Run A, saved results were subsequently checked against the browser display. The earlier browser verification file records only the initial unapproved plan. Run A's later completion is a subsequent state, not a contradiction of that earlier record.

## 9. Tests, CI, and Verification Limits

The target revision passed all 2,715 tests under Python 3.11, including 76 Go2 tests. Public CI also passed under Python 3.11 and 3.13 for the same head. Coverage includes failure when Agent access is unconfigured, the distinction between “configured” and “response observed,” display of Rules rejection, and HTML escaping. Unit-test success was not used as a substitute for delivery completion: Run B separately exercised CLI → Gateway → Agent → physics simulation.

The PR also records earlier validation on `5953588`: fixed-rules delivery, cancellation while yielding, and a forced Gateway termination/restart. These are separate evidence, not the two videos in this report or tests newly run while writing it. Report preparation rechecked existing records; it did not start new model calls or deliveries.

The following remain unverified:

- One map, known scenarios, and one run per scenario do not establish a success rate, reproducibility, worst-case latency, or statistical safety guarantees.
- Navigation relies on ground-truth pose and obstacle information. Lighting, occlusion, detection errors, localization errors, and physical-robot communication delays are not represented.
- Receipt is a logical simulated event. Loading, unloading, actual parcels, safe interaction with real people, and physical locomotion are not validated.
- No WAM, VLA, SimDist, or NavThinker benefit is evaluated.
- There is no matched Agent-versus-simple-rules comparison, so improvements in cost, waiting time, or arrival rate cannot be claimed.
- The map does not update continuously. Approximately 3× video playback, telemetry sampling, and wall-clock inference intervals are different time bases.
- A PR being ready for review is distinct from product-wide release acceptance. Full acceptance of PX4/Gazebo, Nav2/TurtleBot3, and other execution paths is outside this test's scope.

## 10. Reproduction

The target is [public PR #115](https://github.com/pome223/missionos/pull/115), [revision `cf3db6c`](https://github.com/pome223/missionos/tree/cf3db6c462db4cee9dd818f37b62af2011f09646). The corresponding CI execution is [run 36136000626](https://github.com/pome223/missionos/actions/runs/36136000626). MuJoCo 3.13.0 and Torch 2.14.0 are the versions recorded in the run results.

The external model and policy sources are pinned as follows. Their weights are not copied into this report.

- RL-SAR: `376d42c9b128f963ab08579762d5a216a976ce39`, `policy/go2/robot_lab`.
- RL-SAR Zoo: `7dd30bdc7806898950b354260655d5a7f0ce844e`, `go2_description/mjcf`.

Prepare the cache and dependencies using the [instructions at the tested revision](https://github.com/pome223/missionos/blob/cf3db6c462db4cee9dd818f37b62af2011f09646/docs/agents/go2-indoor-delivery.md). Set `VERIFY_ROOT` to a fresh output directory. Do not overwrite previous evidence.

```bash
python -m pip install -e ".[dev]" -e packages/missionos-core \
  -e packages/missionos-cli -e packages/missionos-gateway

# Set DEEPSEEK_API_KEY securely in the host environment beforehand.
# Alternatively, use the launcher's existing Secret Manager integration.
RUN_MISSIONOS_GO2_DELIVERY_SIM=1 RUN_MISSIONOS_GO2_SUPERVISOR=1 \
MISSIONOS_JEV_MODE=off PYTHONPATH=. \
python scripts/start_go2_supervisor_gateway.py \
  --state-dir "$VERIFY_ROOT/gateway" --port 18851
```

In another terminal, omit the CLI supervision-mode option to exercise the default:

```bash
missionos --gateway-url http://127.0.0.1:18851 chat \
  --go2-scenario temporary_blockage 'Go2で会議室Aへ届けて'
# In the same chat: /approve → /run → /status

# Automated reproduction: starts NEW inference and simulation.
RUN_MISSIONOS_GO2_DELIVERY_SIM=1 python scripts/verify_go2_delivery_cli.py \
  --gateway-url http://127.0.0.1:18851 --scenario temporary_blockage \
  --supervision-mode default --require-agent-judgment \
  --output "$VERIFY_ROOT/agent-default"

# Browser: select Run A's scenario, request delivery, approve, and execute.
python scripts/open_go2_chat.py --gateway-url http://127.0.0.1:18851 --port 18852
```

The Japanese request in this command means “Use Go2 to deliver to Meeting Room A.” It is retained exactly as tested; translating the report does not establish support for an English-language request. Obtain the task ID from the created request. Explicitly manage history and session selection when switching contexts. Hosted-Agent responses are not guaranteed to be identical on a rerun.

```bash
missionos --gateway-url http://127.0.0.1:18851 job-status --task-id "$TASK_ID"
missionos --gateway-url http://127.0.0.1:18851 operate --task-id "$TASK_ID"
missionos --gateway-url http://127.0.0.1:18851 watch --task-id "$TASK_ID"
missionos --gateway-url http://127.0.0.1:18851 map --task-id "$TASK_ID" \
  --snapshot --no-open --output "$VERIFY_ROOT/map.html"
```

## 11. Public Materials and Evidence Traceability

This public edition does not depend on a temporary server or Gateway. Keep `index.html`, `index-en.html`, and `media/` in their relative locations to view the videos and recorded replay. Editable text is in `report.md` and `report-en.md`; display data is in `data/replay.json`, and reviewed result excerpts are in `data/results.json`.

The [provenance record](provenance.json) identifies the code revision, SHA-256 hashes of the original result and time-series files, and runtime source hashes. Public case labels `moving` and `agent` replace private run IDs. Approval records, task databases, raw logs, workstation paths, and credentials are excluded. All observed sample points are retained, with only the fields needed for replay. The videos are unchanged original recordings.

The [public file manifest](manifest.json) and [verification script](verify_report.py) check published-file integrity, consistency between result excerpts and displayed values, the decision-linked navigation result, relative links, and numerical agreement between languages. The original private logs are not distributed, so these checks are not an independent third-party rerun of the simulator. The excerpts were checked against the original records during preparation for publication.

For both runs, runtime source hashes matched the corresponding code at the reported revision. The uncommitted changes used in the CLI run were subsequently included in that commit; the final formatting cleanup was checked for AST equivalence. The original evidence archive is retained separately in local storage.

The English edition uses the same videos and numerical dataset as the Japanese edition. Stored Agent rationales remain in their original language, with their meaning explained in English.

## 12. Conclusion and Next Evaluation

Verification went beyond selecting Agent supervision as a default: actual judgments passed Rules checks and led to resumed navigation, simulated receipt, and return. At the same time, short yielding maneuvers around a moving obstacle remained the responsibility of local control, without an Agent call.

A useful next performance test would **compare the Agent with fixed rules under the same closure duration, notices, observations, and action envelope**. A simple rule that uses the reopening estimate should be a strong comparator. Measure arrival, undelivered return, waiting, wall-clock latency, and inference cost under matched conditions. First establish whether selecting actions optimally leaves room for improvement; do not keep running inference where no meaningful difference is possible. This additional experiment was not performed for this report.
