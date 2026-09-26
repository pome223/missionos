# Go2 indoor delivery simulation

## Scope and authority

The new `Go2DeliveryMission` coordinator sends one destination per leg to a
MuJoCo simulator executor. It reuses MissionOS's validated
`HardwareAdapterEvidence` contract with `execution_mode=sim`,
`adapter_kind=vendor_specific` and `completion_scope=sim_action` per leg.
It does not identify this executor as Nav2 or Unitree's Sport API.

One operator-authorized, immutable plan binds pickup, destination, parcel,
requested speed and at most one retry per leg to a SHA-256 digest. Standalone
episodes use an explicit approval reference. The normal Gateway conversation
also binds approval to its registered context, owning session, scenario and
model/policy file hashes, and saves proposals, approval and progress in TaskStore.

This prototype connects to the normal Gateway conversation, CLI chat and job-status.
The Gateway conversation and browser default to `agent`; CLI chat inherits the
Gateway default when no supervision mode is specified. Agent mode uses a dedicated
MissionOS ADK supervisor through the existing agent Runner and invocation-evidence
contract, with a Go2-specific deterministic gate. Explicit `rules` mode makes no
LLM call and remains the standalone simulator runner's baseline default. Agent
configuration alone does not imply that a judgment was invoked. This does not claim
integration with the persistent Mission Assurance policy graph. The learned
locomotion network runs locally and has no mission approval authority.

## Reproduce

Requires Python 3.11+ and Git. Tested with Python 3.13 on Apple Silicon, CPU only.
The optional environment pins MuJoCo 3.13.0 and Torch 2.14.0. Video recording
also requires an OpenGL context. Linux headless video may require an appropriate
MuJoCo graphics backend; it was not tested in this task.

```bash
RUN_MISSIONOS_GO2_DELIVERY_SIM=1 \
GO2_DELIVERY_OPERATOR_APPROVAL_REF=YOUR_SIMULATION_APPROVAL_REF \
GO2_DELIVERY_OUTPUT=output/go2-delivery/my-baseline \
bash scripts/run_go2_delivery_local.sh \
  --mission-id my-baseline --simulate-recipient --video
```

The output directory must be empty. Set `GO2_DELIVERY_CACHE` to override the
external-source/environment cache. The wrapper downloads dependencies and two
pinned public sources; it refuses a modified or different cached checkout:

- [rl_sar](https://github.com/fan-ziqi/rl_sar),
  `376d42c9b128f963ab08579762d5a216a976ce39`, `policy/go2/robot_lab`.
- [rl_sar_zoo](https://github.com/fan-ziqi/rl_sar_zoo),
  `7dd30bdc7806898950b354260655d5a7f0ce844e`, `go2_description/mjcf`.

Upstream models, weights and notices stay in external checkouts, not in this
repository. No cloud resource, hardware interface, ROS connection or robot network
control endpoint is created. Downloads occur during setup only.

### Normal chat and browser

After preparing the simulator cache above, use a MissionOS Python environment
with the repository's Gateway/CLI dependencies installed. Start a fresh Gateway
from this checkout after every runtime code change. These commands use a separate
task database and explicit simulator opt-in; they do not enable hardware or SITL.
Agent supervision is the default. Supply `DEEPSEEK_API_KEY` in the host environment,
or add `--secret-project "$GO2_SECRET_PROJECT" --secret-id "$GO2_SECRET_ID"` using
an authorized existing Secret Manager locator. The launcher validates configuration
before listening. Credentials are never passed to simulator children or the browser.

```bash
RUN_MISSIONOS_GO2_DELIVERY_SIM=1 RUN_MISSIONOS_GO2_SUPERVISOR=1 \
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-gateway/src \
python scripts/start_go2_supervisor_gateway.py \
  --state-dir output/go2-agent --port 18791
```

In another terminal:

```bash
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-cli/src \
python -m missionos_cli --gateway-url http://127.0.0.1:18791 \
  --state-path output/go2-chat/cli-state.json chat '会議室Aへ届けて'
```

Use `/approve`, then `/run`. `/status`, `状況` and `/job-status` report the same
task; `/cancel` or `中止` requests cancellation. `承認して開始` combines the explicit
approval and start steps. This catalog accepts meeting room A only; it does not
interpret arbitrary delivery destinations. Agent mode applies to execution exceptions.

For the moving-obstacle simulator run, opt in explicitly with `chat
--go2-scenario moving_obstacle --go2-supervision-mode rules`. The rules mode
uses the existing local velocity guard; WAM forecasts do not authorize or steer
this delivery. The CLI stores the Go2 task ID after `/run`. Use `/job-status` in
chat, or `missionos job-status --task-id <id>`, `missionos operate --task-id
<id>`, `missionos watch --task-id <id>`, and `missionos map --task-id <id>
--snapshot --no-open`. Go2 `operate` and `watch` are read-only displays, and the
map is a static indoor snapshot of the known simulator geometry and observed
trajectory. These views do not claim physical delivery.

The optional browser client uses that same production conversation route.
Its fresh-page default is `agent`, using the Agent-enabled Gateway above.
An unconfigured Agent is an explicit error; it never silently becomes rules mode.

```bash
PYTHONPATH=.:packages/missionos-core/src \
python scripts/open_go2_chat.py --gateway-url http://127.0.0.1:18791
```

Open the loopback URL printed by the client. It shows actual simulator frames,
phase changes, simulated receipt, return verification and result/video links.
If `GATEWAY_API_KEY` is configured, give the client the same environment variable;
the key stays server-side. The client requires an exact loopback Host/Origin and
an unpredictable page token for writes. It forwards only the Go2 catalog for one
server-created session. Gateway authentication remains unchanged. Read-only
media is restricted to `live.jpg`, `delivery.mp4`, and `result.json` under a known
Go2 task, using the Gateway's existing shared operator authentication scope.

### Chat execution contract

- `go2_delivery_fixed_catalog` routes before the general dialogue router.
  Proposing and approving never start a process. `/run` requires the server-owned
  approval and `RUN_MISSIONOS_GO2_DELIVERY_SIM=1` at the Gateway.
- Revising a proposal supersedes its old approval. Foreign sessions, tampered or
  expired context references, and changed model/policy files cannot dispatch it.
- One simulator worker runs at a time. Repeated `/run` for that task is idempotent.
  TaskStore receives `starting` before worker creation, then observed progress
  and final evidence. The result must match the approved proposal and plan hashes.
- `/cancel` first records `cancel_requested`. Navigation observes a cancellation
  file, stops velocity requests and settles. `canceled` requires observed upright
  state and speed below 0.06 m/s; an unverified stop remains `needs_attention`.
- Completion requires the runtime's receipt and terminal-hold evidence; an exit
  code, phase label, command ACK or video alone cannot establish completion.
- Plans and outcomes are durable; the Gateway's context registry and retry budget
  within a physics episode are process-local. Gateway restart does not resume or
  replay execution. On restart, interrupted tasks become `needs_attention` and
  receive a cancellation file in their recorded output root. New dispatch is
  blocked until the previous worker's process-lifetime lock is released. The
  lock is acquired before launch and inherited by the child, closing the spawn
  window even if the Gateway dies before recording the child PID. Lock identity
  includes the original device/inode; missing or replaced files are not stop
  evidence. A second restart preserves the fence, including records beyond the
  newest 100 tasks. Do not delete lock files while workers may exist.
  Worker exit only proves that simulator execution ended; it does not verify
  mission success or a physically settled robot. A fresh plan/approval is needed.
  Pre-lease records or unreadable evidence remain blocked for explicit operator
  investigation; no automatic or PID-only unlock is provided. Old conversation
  contexts remain invalid after restart; automatic stop requests do not depend
  on reusing them. Graceful shutdown also requests cancellation.
- Browser context survives page reloads within the same tab through session
  storage. Restarting the browser client creates a new operator session; CLI
  `job-status` and TaskStore retain recorded results. This is a local prototype, not a multi-operator fleet UI.

Use `--scenario blocked_passage` for one blocked passage, or
`--scenario all_blocked` for both passages. Give each run a new output directory.
The first outbound route is disturbed after six simulated seconds. This is a
controlled scenario, not a held-out performance benchmark or perception test.

## Execution and observation contract

- The office is 8 by 6 metres. Reception is `(-2.5, 0)`; meeting room A is `(2.5, 0)`.
  A central divider offers two passages. A* uses a 0.1 m grid and 0.45 m clearance.
- Navigation receives the simulator pose and known obstacle geometry. There is
  no simulated SLAM, camera recognition or lidar-based obstacle detection.
- The path follower converts a final goal into heading and speed requests.
  The requested translation speed is capped at 0.25 m/s. This is a command
  limit, not a hard bound on every measured instantaneous velocity.
- The external 45-observation locomotion policy runs at 50 Hz, using body angular
  velocity, projected gravity, velocity commands, joint state and prior actions.
  Torque-limited PD runs at 200 Hz (`kp=20`, `kd=0.5`, torque ±23.5 Nm).
- MuJoCo integrates a free Go2 body, articulated joints, gravity and contacts.
  Robot pose is initialized once; navigation never writes its position directly.
  Scenario carts are moved by the harness and explicitly identified as disturbances.
- Arrival requires goal-executor success, observed robot motion and fresh,
  upright simulator state within 0.4 m of the approved endpoint. The executor's
  own positional tolerance is 0.25 m. After a two-second zero-velocity hold,
  it reobserves position and corrects drift under the original timeout. The inner
  0.7× target only starts settling; it does not relax the success tolerance.
  Cancellation and latched safety violations take precedence immediately after
  the hold, even when the hold crosses the navigation deadline.
- Arrival produces `awaiting_receipt`, not delivery completion. A receipt must
  bind mission, destination and parcel and be issued after verified arrival.
- `--simulate-recipient` inserts an explicitly labelled simulated receipt after
  three seconds. There is no physical parcel, loading/unloading or payload dynamics.
  Without this flag, the episode exits as `awaiting_receipt`; persisted receipt
  resumption is not implemented.
- After receipt, the same mission authorization covers the return destination.
  A clean navigation abort permits one wait and retry of that destination.
  Fall/contact/geofence violations latch and prevent further dispatch.
- Completion is emitted only after the return and a five-second stability hold.
  A failed or blocked mission remains `needs_attention`; it cannot claim completion.

## E2E / Runtime Verification

The local runtime was exercised in three fresh physics episodes on 2026-09-22:

| Scenario | Observed mission result | Recovery |
| --- | --- | --- |
| Normal route | Simulated receipt and return completed | None |
| First passage blocked | Simulated receipt and return completed | One bounded retry, using the other passage |
| Both passages blocked | Stopped with `needs_attention`; no receipt or return | One retry exhausted with no route |

The baseline used the wrapper above, including fresh cache/environment setup.
The other scenarios use the same command with the scenario flag. Each episode
writes `result.json`, `events.jsonl`, and actual 10 Hz simulator telemetry.
With `--video`, `delivery.mp4` combines actual MuJoCo rendering with labelled
telemetry/map overlays at 3x playback. Results include model, policy and runtime
source hashes. Do not substitute an ACK, fixture result or rendered image for
these trajectory, receipt and terminal-hold checks.

```bash
PYTHONPATH=.:packages/missionos-core/src python -m pytest \
  tests/contract/test_go2_delivery_mission.py tests/contract/test_go2_delivery_chat.py -q
```

These fixtures cover approval binding, actual-arrival versus ACK, receipt timing,
retry bounds and stability-before-completion. They do not validate walking.
The earlier CHAMP/Gazebo attempt could not produce reliable walking and did not
complete delivery; the verified backend is the MuJoCo/RL configuration above.

The normal CLI was also exercised against a real loopback Gateway: planning,
`/approve`, `/run`, and `/job-status` retained one task ID and completed the actual
74.56-second simulated delivery. The browser client separately completed the
same route with live images, simulated receipt and return verification. These
are separate physics executions, not replays of the earlier videos.

Observed chat-path episodes on 2026-09-22:

| Entry / scenario | Terminal observation |
| --- | --- |
| Browser / normal delivery | `completed`, 74.56 simulated seconds, receipt plus verified return |
| Browser / one passage blocked | `completed`, 87.34 simulated seconds, one retry, receipt plus verified return |
| HTTP chat / both passages blocked | `needs_attention`, 21.01 simulated seconds, no receipt or completion |
| HTTP chat / operator cancellation after motion | `cancel_requested` then `canceled`, final measured speed below 0.001 m/s |

Reloading the browser during the blocked-passage mission retained the task ID,
scenario and controls, then observed completion. The CLI's `/status` reported
the completed task and `/cancel` after completion did not launch or change it.
Browser requests with missing tokens, foreign Origin or foreign Host returned
403; a non-allowlisted artifact returned 404. This runtime check used the existing
loopback non-browser authentication mode, without a configured Gateway API key.

For repeatable real HTTP boundary checks, start the opted-in Gateway and run:

```bash
RUN_MISSIONOS_GO2_DELIVERY_SIM=1 python scripts/smoke_go2_delivery_chat.py \
  --gateway-url http://127.0.0.1:18791 \
  --output output/go2-chat/blocked-smoke.json --scenario blocked_passage
RUN_MISSIONOS_GO2_DELIVERY_SIM=1 python scripts/smoke_go2_delivery_chat.py \
  --gateway-url http://127.0.0.1:18791 \
  --output output/go2-chat/cancel-smoke.json --cancel-after-motion
RUN_MISSIONOS_GO2_DELIVERY_SIM=1 python scripts/smoke_go2_delivery_chat.py \
  --gateway-url http://127.0.0.1:18791 \
  --output output/go2-chat/all-blocked-smoke.json --scenario all_blocked
```

Each command explicitly approves one simulator test episode, checks refusal
before approval and for foreign/tampered context, tries duplicate dispatch,
polls the same task, and verifies the actual terminal result. Use new output
filenames to retain evidence. This does not validate real hardware, payload
handling, navigation perception or learned mission-level judgment.


## Default Agent supervision

Prepare the simulator cache and MissionOS dependencies as above. Explicitly start
the Agent-enabled Gateway with an operator-supplied Secret Manager locator. The
launcher loads the key into host memory without printing or writing it. Omit the
two secret flags when `DEEPSEEK_API_KEY` is already in the host environment.
The simulator child receives an allowlisted environment without API credentials.

```bash
RUN_MISSIONOS_GO2_DELIVERY_SIM=1 RUN_MISSIONOS_GO2_SUPERVISOR=1 \
PYTHONPATH=.:packages/missionos-core/src:packages/missionos-gateway/src \
python scripts/start_go2_supervisor_gateway.py \
  --state-dir output/go2-agent --port 18791 \
  --secret-project "$GO2_SECRET_PROJECT" --secret-id "$GO2_SECRET_ID"
```

Use the browser command above; `MissionOS Agentが状況を判断` is already selected. The
proposal binds `supervision_mode`, the selected `missionos_go2_supervisor_agent`,
`deepseek-flash` connection and action envelope. Missing or changed model
configuration does not fall back to fixed-rule execution.

- Each mission permits at most three judgments (25-second model timeout, 768
  output tokens each), up to 20 simulated seconds of selected waiting, one retry
  per navigation leg, and an undelivered return to the existing home goal. Physics
  holds position at approximately real-time pace during inference. This separate
  judgment hold is bounded to 35 wall seconds per call.
- Only current pose/health, known-map route availability, facility-notice estimates,
  remaining budget and prior judgments enter the prompt. Scenario names, future
  action tapes, future robot trajectories and terminal results are not supplied.
- The host response binds the observation hash. Rules reject unknown fields or
  actions, expired replies, changed maps, excessive pose drift, unavailable paths,
  cancellation, unsafe state and exhausted budgets. Model proposals cannot expand
  approval or change the destination. Failure stops for operator attention.
- Waiting is followed by a new physical observation and a new judgment. Rerouting
  recomputes a path to the same goal and can use a reopened passage. Navigation
  evidence names its `source_decision_id`. Undelivered return requires verified
  home arrival and stability; `returned_undelivered` cannot claim delivery.
- `temporary_blockage` closes both passages six outbound seconds into the episode
  and removes the physical carts twelve simulated seconds later. A labelled
  simulated facility notice supplies an estimated reopening time. This is a
  controlled integration case with outcome headroom over the original fixed
  five-second retry, not an unseen-environment or perception benchmark. A rule
  using the same reopening notice could also choose to wait; the comparison does
  not establish LLM superiority.

Real HTTP/physics verification commands (use fresh output filenames):

```bash
RUN_MISSIONOS_GO2_DELIVERY_SIM=1 python scripts/smoke_go2_delivery_chat.py \
  --gateway-url http://127.0.0.1:18791 --supervision-mode agent \
  --scenario temporary_blockage --output output/go2-agent/temporary-smoke.json
RUN_MISSIONOS_GO2_DELIVERY_SIM=1 python scripts/smoke_go2_delivery_chat.py \
  --gateway-url http://127.0.0.1:18791 --supervision-mode agent \
  --scenario all_blocked --output output/go2-agent/return-smoke.json
```

Use `--supervision-mode rules` for the original fixed-rule comparison. Contract
fixtures in `test_go2_supervision.py` cover invalid proposals, observation replay,
cancellation/map changes, cumulative budgets, model failure, and undelivered
return semantics. Fixtures are not model or walking evidence. Runtime artifacts
retain actual ADK invocation hashes, model identity, judgments, gate results,
navigation evidence and physical telemetry.

## Moving obstacle scenario

Choose `動く障害物を避けて配送` in the browser, or `--scenario moving_obstacle`.
The disturbance harness moves one 0.24 m radius cylinder at 0.2 m/s from
`(-1.25, 1.25)` to `(-1.25, -2.6)`, starting at simulation time 3 s. It then
parks beside the wall. Only the obstacle pose is prescribed. The Go2 still
moves through the learned policy, joint torque and MuJoCo integration.

The executor reads obstacle geometry positions at 20 Hz and estimates velocity
from the previous sample. Navigation has no access to the trajectory's future
positions, turning points or ending time. Dynamic samples do not alter the
static map revision used by the mission judgment guard.

Before each 200 Hz physics step in active navigation, a local velocity guard
checks closest approach over four seconds of requested/intended motion and
0.75 seconds of measured inertia. It uses a declared 0.45 m robot navigation
radius, the 0.24 m obstacle radius, and a 0.18 m margin. This envelope is a
navigation approximation, not a certified bound on every articulated body part.
Missing, invalid, future-dated or older-than-0.15-second tracks stop translation.
The intended route remains part of the check while turning or holding, so a
zero present velocity alone cannot authorize resumption.

- `dynamic_yield_requested` records the observation and predicted conflict.
- `dynamic_stop_observed` requires measured speed below 0.06 m/s for 0.3 s;
  it records elapsed time and measured travel after the stop request. This is
  separate from the request and is not an instantaneous-stop claim.
- `dynamic_path_clear_observed` requires a conflict-free candidate for 0.75 s
  and a previously observed stop, before resuming the same approved goal.
- Each uninterrupted local yield is bounded to 20 simulated seconds. Exhaustion
  returns a navigation abort to the existing bounded mission recovery. It does
  not grant a new destination or silently bypass the local guard on a retry.
- Operator cancellation is checked during yielding. Completion still requires
  the recipient receipt, home arrival and the five-second terminal hold.

These local responses are executor rules, not LLM decisions. Agent mode remains
available for prolonged mission exceptions; the completed crossing scenario
requires no hosted model invocation. `dynamic_avoidance` in `result.json` stores
obstacle travel, yield/stop/resume events, minimum center separation, separation
minus the two declared radii, and contact counts at every physics step. The
reported clearance is an envelope calculation, not mesh-to-mesh distance.
Moving-obstacle contacts participate in the existing latched safety violation.
The video shows the actual red cylinder, with a position/velocity map overlay.

Runtime commands against a freshly restarted, opted-in Gateway:

```bash
RUN_MISSIONOS_GO2_DELIVERY_SIM=1 python scripts/smoke_go2_delivery_chat.py \
  --gateway-url http://127.0.0.1:18791 --supervision-mode agent \
  --scenario moving_obstacle --output output/go2-moving/crossing-smoke.json
RUN_MISSIONOS_GO2_DELIVERY_SIM=1 python scripts/smoke_go2_delivery_chat.py \
  --gateway-url http://127.0.0.1:18791 --supervision-mode agent \
  --scenario moving_obstacle --cancel-during-yield \
  --output output/go2-moving/cancel-smoke.json
```

The first checks the production chat/approval/worker/physics boundary, measured
obstacle movement, yield then observed stop then clear path, zero moving-object
contacts, positive envelope clearance, receipt and verified return. The second
requests cancellation during a local yield and checks observed stop without
resumption or delivery completion. Fixture tests in `test_go2_dynamic_obstacles.py`
cover crossing/receding motion, inertia, intended-path checks while stopped,
observation freshness, finite-difference readback and cancellation.

This is one controlled crossing with ground-truth observations. It does not
establish performance for multiple people, arbitrary adversarial trajectories,
head-on pursuit, sensor errors, or real hardware. Avoiding here means yielding
and resuming; routing around an independently moving person is not implemented.

Observed on 2026-09-22 with the commands above and a separate browser execution:

| Boundary / scenario | Observed result |
| --- | --- |
| Browser and HTTP chat / crossing | Both completed in 84.43 simulated seconds, with mock receipt and verified return |
| Moving obstacle readback | 3.85 m traveled; zero contact physics steps; minimum declared-envelope clearance 0.546 m |
| Local yield | Two holds (0.82 s and 8.63 s); measured stop confirmed 0.47 s and 0.46 s after the requests |
| HTTP chat / cancellation during yield | `canceled`, no receipt or completion, zero contacts; final speed below 0.001 m/s |

The full related contract suite passed 191 tests. The runtime runs used the
external pinned Go2 model and walking policy; these counts do not expand the
single-crossing scope described above.

## Public integration verification (2026-09-25)

A fresh clone of public `main` received the Go2 delivery and arrival corrections.
An isolated environment was installed from its public packaging files:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]" -e packages/missionos-core \
  -e packages/missionos-cli -e packages/missionos-gateway
```

Prepare the pinned simulator cache using the opt-in setup above, then start a
fresh Gateway with a separate task database and output root:

```sh
RUN_MISSIONOS_GO2_DELIVERY_SIM=1 MISSIONOS_GO2_SUPERVISION_MODE=rules \
MISSIONOS_JEV_MODE=off TASK_STORE_DB_PATH="$VERIFY_ROOT/tasks.db" \
MISSIONOS_GO2_OUTPUT_ROOT="$VERIFY_ROOT/delivery" \
python -c 'from src.gateway.server import create_missionos_gateway; create_missionos_gateway().run(host="127.0.0.1", port=18843)'
```

In another terminal, with the installed CLI environment on `PATH`:

```sh
RUN_MISSIONOS_GO2_DELIVERY_SIM=1 python scripts/verify_go2_delivery_cli.py \
  --gateway-url http://127.0.0.1:18843 --output "$VERIFY_ROOT/positive"
RUN_MISSIONOS_GO2_DELIVERY_SIM=1 python scripts/verify_go2_delivery_cli.py \
  --gateway-url http://127.0.0.1:18843 --cancel-during-yield \
  --output "$VERIFY_ROOT/cancel"
```

`VERIFY_ROOT` must name a fresh directory outside the checkout. The helper invokes
the installed `missionos` executable, rather than substituting direct HTTP calls
for chat. Each run independently checks proposal, rejection of unapproved `/run`,
approval, dispatch, observed motion, and terminal results through `chat`,
`job-status`, `operate`, `watch`, and a generated `map` for that same task.
`operate` and `watch` are also captured while the task is running.

| Scenario | Observed result | Simulated time | Obstacle-contact steps |
| --- | --- | --- | --- |
| Moving obstacle, receipt, return | `completed`; two yields; receipt and terminal hold verified | 87.39 s | 0 |
| Cancel while yielding | `canceled`; completion false; measured terminal speed below 0.06 m/s | 13.805 s | 0 |

The completed map was visually checked in a browser: the indoor divider, observed
return trail, home/destination, obstacle, and `completed` status rendered. Raw
task IDs, databases, logs, external weights and workstation paths are not included
in this publication. The script writes local evidence for reproduction.

Limits: rules mode and actual RL-SAR/MuJoCo locomotion, with simulator-truth
geometry and a simulated recipient. No hosted LLM, WAM, SimDist, NavThinker or
physical hardware was invoked in these public integration runs. Previously
documented Agent-mode runs are separate historical evidence. PX4/Gazebo and
Nav2 live simulators were not rerun; this is not full Level C release acceptance.
Do not infer release-wide readiness from this bounded Go2 verification.

### Review regression verification (2026-09-25)

The orphan-worker and fresh-browser-default regressions failed before the fix.
The revised suite passes 73 Go2 tests, including a live child retaining an
inherited lease, release after child exit, the pre-spawn window, repeated restart,
legacy `needs_attention` records, changed output roots, older pages, and
missing/replaced locks. These fixtures do not execute robot physics.

A separate opt-in probe exercises the actual simulator and installed CLI:

```sh
RUN_MISSIONOS_GO2_DELIVERY_SIM=1 PYTHONPATH=. \
  python scripts/verify_go2_gateway_restart.py \
  --port 18845 --output "$VERIFY_ROOT/restart"
```

It starts its own fresh Gateway/database, observes physics progress, pauses only
its simulator child with `SIGSTOP`, kills that Gateway, and restarts it. The new
approved delivery is rejected while the old child retains its lock; the stop
file appears in the original output root. After `SIGCONT` and child exit, the
same approved new task may dispatch, then is canceled for cleanup. The interrupted
mission remains unverified. Old conversation context rejection is also preserved.
The injected process signals are fault simulation, not ordinary control behavior.

This probe passed. The normal delivery CLI probe was also rerun on a separate
fresh Gateway and completed in 87.39 simulated seconds, with two yields, zero
obstacle contacts and consistent results across all five operator surfaces.
At that review revision, a fresh browser page selected rules and produced an
unapproved plan. That default was subsequently superseded by the operator-requested
Agent default below; current normal startup uses the Agent-enabled launcher.
The full Python 3.11 suite passed 2,712 tests. Release-wide Level C limits above
remain unchanged.

### Agent-default CLI verification

The browser and Go2 conversation default to Agent supervision. The direct simulator
runner retains its explicit rules baseline, and CLI comparisons may request
`--go2-supervision-mode rules`. Rules-only Gateway operation must explicitly set
`MISSIONOS_GO2_SUPERVISION_MODE=rules`; it is not a fallback for missing Agent access.

An unobstructed run or a moving-cart yield can finish without a mission-level Agent
judgment. To verify actual judgment use, select the temporary passage closure:

```sh
missionos --gateway-url http://127.0.0.1:18791 chat \
  --go2-scenario temporary_blockage 'Go2で会議室Aへ届けて'
```

Use `/approve`, `/run`, then `/status`. The default model can propose a bounded
wait, reroute, undelivered return, or operator attention. Only Rules-admitted
decisions may continue under the approved mission. `status` is the Gateway health
command; `job-status --task-id <id>` is the task status command.

The reproducible installed-CLI probe leaves the mode option unset and requires
real DeepSeek response evidence plus a decision-linked verified navigation leg:

```sh
RUN_MISSIONOS_GO2_DELIVERY_SIM=1 python scripts/verify_go2_delivery_cli.py \
  --gateway-url http://127.0.0.1:18791 --scenario temporary_blockage \
  --supervision-mode default --require-agent-judgment \
  --output "$VERIFY_ROOT/agent-default"
```

`job-status`, `operate`, `watch` and `map` distinguish configured supervision from
observed model responses, and show the action, rationale and Rules verdict for
each decision. `map` remains a generated snapshot, not a continuously updating map.

Observed on 2026-09-25 using the default mode: two real `deepseek-flash` responses
selected `wait`, then `reroute` after reobservation. Both passed Rules; the second
decision was linked to a verified outbound navigation leg. Simulated receipt,
return and terminal hold completed in 95.45 simulated seconds with zero obstacle
contacts. All five CLI surfaces agreed on the task and terminal outcome;
`job-status`, `operate`, `watch` and the map showed both decisions. The separate
`status` command reported a healthy Gateway. A fresh browser page kept Agent
selected, and Send created an unapproved plan without changing the selector.
The completed map was visually checked for the return trail, destination/home
and both judgment rationales.

This run invokes the hosted Agent and real simulator physics, but does not test
WAM or physical delivery. It uses known simulator geometry/pose and a facility
reopening notice. It establishes the governed integration, not superiority over
a matched rules comparator. Raw tasks, credentials and logs remain local.

The final Python 3.11 suite passed 2,715 tests (76 Go2 tests). Lint, both
versioned evidence gates, the 114-entry smoke inventory, outgoing publication
checks and relative documentation links passed.

### Published technical report and recordings

The detailed [Japanese report](../assets/go2-agent-delivery-20260925/report.md) and
[English report](../assets/go2-agent-delivery-20260925/report-en.md) include the
Agent-default CLI run and a separate browser moving-obstacle run. Both configured
Agent supervision; only the temporary-closure run actually invoked the LLM.
The [report bundle](../assets/go2-agent-delivery-20260925/README.md) includes both
recordings, standalone HTML replay, reviewed result excerpts, and integrity checks.
The scenarios differ and do not constitute an Agent-versus-rules comparison.
