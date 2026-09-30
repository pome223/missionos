# Yokohama harbour delivery from MissionOS chat

The Yokohama PX4/Gazebo delivery runs from the normal MissionOS conversation
route. It follows the Go2 delivery pattern:

1. A DeepSeek planner interprets the request against a one-route catalog.
2. The operator approves the exact plan in chat.
3. A Gateway worker launches the simulator.
4. Every verifier must pass before the task is reported complete.

This is simulation only; no hardware is used.

## Flow

| Chat turn | What happens |
|---|---|
| `横浜の配送パッドへ荷物を届けて` | `missionos_yokohama_delivery_planner_agent` (DeepSeek) returns `supported`, `destination_id`, `summary` and `reason`. Only `yokohama_harbour_pad` is accepted. The Gateway builds a proposal from fixed server data: route, city-model backend, pad-queue limits, simulator arguments, input hashes and agent configuration. |
| `/approve` | Records the approval, bound to the proposal digest and the session. |
| `/run` | Rechecks the proposal digest, input hashes, simulator arguments and agent configuration. It refuses if another `missionos-yokohama-*` container is running, then launches `scripts/yokohama_sitl.py` with `--approval-manifest`. |
| `/status` | Reports the current stage, the latest pad-judge rationale and the verifier results. |

The `yokohama_sitl.py` arguments are:

```
--phase flight --sea-round-trip --deliver-payload --occupied-pad
--pad-state-advisory assist --pad-mission-judge gateway
--decision-backend {fixture|native} --wam-profile motion-v4 --timeout-seconds 3000
```

The simulator process receives no model API keys or Gateway credentials. After
it exits, the Gateway runs the five verifiers: decisions, pad_queue,
pad_advisory, payload and sitl. The task is `completed` only if the run passed
and all five verifiers passed. Otherwise it is `needs_attention`.

## Gateway environment

| Variable | Meaning |
|---|---|
| `RUN_MISSIONOS_YOKOHAMA_AGENTS=1` | Enables the two DeepSeek agents. Also requires `DEEPSEEK_API_KEY` and the DeepSeek LiteLLM provider. |
| `RUN_MISSIONOS_YOKOHAMA_DELIVERY_SIM=1` | Enables `/run`. |
| `MISSIONOS_YOKOHAMA_SITL_PYTHON` | Python environment for the simulator and verifiers. |
| `MISSIONOS_YOKOHAMA_CITY_MODELS` | `fixture` (default, no GPU) or `native`. |
| `MISSIONOS_YOKOHAMA_NATIVE_SERVICE_CONFIG` | Service file, required for `native`. GPU provisioning stays outside the Gateway. |
| `MISSIONOS_YOKOHAMA_OUTPUT_ROOT` | Run directory (default `output/yokohama-chat`). |

Supply the key from a secret store at process start; never write it to a file
in the repository.

## Pad mission judge

`--pad-mission-judge gateway|fixture` adds `world.pad_queue.mission_judge`:

- `max_decisions=2`
- `max_added_wait_s=30`
- `judge_timeout_s=20`

It requires `--pad-state-advisory assist`, which keeps a 2 s request cadence
and supplies the CPU forecast. It is refused with `--pad-approach-decision`.

**Host side (`MissionJudgeGate`).**

- The judge is asked only when the Rules/advisory action is
  `enter_delivery_approach`. The request, `pad-judge/NNN/request.json`,
  contains:
  - pad facts: the clear window, the lead's distance from the pad and its
    change, the lead's altitude and battery;
  - the advisory signal;
  - the remaining wait budget.
- While the answer is pending, the aircraft is told to wait. A valid `wait`
  holds for at most the remaining budget. A valid `enter` becomes
  `no_objection`.
- The following fall back to the Rules action: a timeout (`unavailable`), a
  malformed, unreadable or unbound answer (`invalid`), an exhausted budget,
  or an exhausted decision count.
- Only judge-caused waiting is charged: runs of `pending`/`hold` responses,
  measured on the observation clock and a monotonic host clock, whichever is
  later. The answer is re-timed after it is read. The gate releases at
  `max_added_wait_s - response_max_age_s` (28 s), so the executor can accept
  the release before the 30 s budget ends.
- If the pad becomes unclear, the Rules wait closes the run. Its time is not
  charged to the judge, and pending or held judgments are discarded.

**Gateway side.** The worker answers each request once, with
`missionos_yokohama_pad_judge_agent` (DeepSeek), and records the decision and
rationale on the task. The executor's Rules never depend on this answer.

The simulator and each verifier run in their own process session. Completion,
`/cancel` and Gateway shutdown interrupt the process and then kill its process
group; cleanup has a grace period of up to 180 s. Descendants that start
another session, and Docker-daemon containers, are not proven to be reaped by
this path. `execute` still refuses to start while a `missionos-yokohama-*`
container is running.

**Executor side (`judge_overlay` in `require_response`).**

- The receipt's `prior_action` must equal the recomputed Rules/advisory action.
- A Rules wait must be `not_consulted`.
- A changed action can only turn entry into a wait, and only within the budget.
- During a judge-caused run, the receipt carries `wait_deadline_wall_s`. The
  aircraft refuses a judge wait at or after that deadline, refuses a release
  that arrives after it, and aborts if no response comes before it. A Rules
  wait or an entry clears the deadline. Exceeding the deadline stops the run;
  it does not guarantee a timely release under processing delay.

`verify_yokohama_pad_queue.py` also requires that:

- every receipt matches its request/response record by hash;
- the judge only added waiting;
- the judge stayed within its budget and decision count. The elapsed time is
  reconstructed from the observations, up to the aircraft's receipt of the
  release. The receipts' own totals are not trusted.

`verify_yokohama_pad_advisory.py` binds its fixture judgment to the pre-judge
action (`prior_action`).

## Observed chat run (2026-09-30, fixture city models)

The production Gateway ran on `127.0.0.1:18791`, with the key loaded from a
secret store into the process environment. Chat turns were sent over HTTP to
`/missionos/autonomy-conversation/run`. The task was
`yokohama_ef18ddcee96748f3` and the simulator run was `yokohama-78d588052d40`.

- **Planner:** DeepSeek (`deepseek-v4-flash`) accepted the request and wrote
  the plan summary. `/approve` then `/run` launched the flight.
- **City steps:** D1 and D2 were reached with the fixture city models.
- **Pad queue:** the pad was reported occupied at 618 s. When the Rules first
  allowed entry, the judge was asked and answered `wait` 6 s. It was asked
  again and answered `wait` 21 s, the remaining budget. At 31 s after the
  first judge-caused wait, the budget was exhausted and entry came from the
  Rules at 683 s. The total pad wait was 61 s of simulation time.
- **Budget overrun:** this run predates the release margin. The current
  verifier measures 31.3 s of judge-caused waiting up to the received release,
  which is over the 30 s budget, so `mission_judge_within_budget` now fails for
  this record.
- **Completion:** cargo was received and the aircraft landed on the ship. All
  five verifiers passed: decisions, pad_queue (23 checks, including the four
  mission-judge checks), pad_advisory, payload and sitl. `/status` then
  reported `completed`.

The first judge rationale said the lead might still be inside the 6 m pad
radius while it was 11.4 m away. The executor's Rules did not depend on that
statement, which is why the judge may only add a bounded wait.

## Observed chat run with native city models (2026-09-30)

The Gateway ran with `MISSIONOS_YOKOHAMA_CITY_MODELS=native` and a service file
for a one-attempt L4 VM. The operator planned and approved task
`yokohama_70651db0feaa4c25` in chat. Once the VM was bootstrapping and the
loopback tunnel was open, the cost-capped GPU controller sent that session's
`/run`. A first GPU session ran idle and never started a flight, because the
monitoring pipeline buffered the "ready" signal. The controller now sends
`/run` itself.

- **D1 and D2:** native AeroVLA took about 17 s per cycle and ANWM motion-v4
  about 50 s. Both passed the city WAM gate, with final target errors of 0.10
  and 0.05 m. The session was revoked at D2, before the pad wait.
- **GPU:** the GPU was deleted after the verified model stop, 16.4 minutes after
  creation. Estimated cost was $0.53.
- **Pad queue:** the pad was reported occupied at 934 s. The judge answered
  `wait` 5 s, then `wait` 18 s. The budget was exhausted and the Rules granted
  entry at 999 s, after a total pad wait of 62 s of simulation time. Like
  the fixture run, this run predates the release margin. The current verifier
  measures 31.6 s of judge-caused waiting, over the budget.
- **Completion:** cargo was received at 1169 s and the aircraft landed on the
  ship at 1760 s.
- **Verification:** all five verifiers passed. The decisions verifier set
  `native_model_flight_verified=true`. The pad_queue verifier ran 23 checks,
  including the stopped city models and the four mission-judge checks.
  `/status` reported `completed`.

The first judge rationale again doubted that the lead had left the 6 m radius
while it was 11.3 m away. It also stated that entry needs human confirmation,
which is not this run's authority model: entry comes from the Rules inside the
approved plan.

## Runtime check after the timing fixes (2026-09-30, fixture city models)

The same chat flow ran on the fixed code, with task `yokohama_745b51c0b68b4998`
and simulator run `yokohama-c156d9f5821a`.

- The judge answered `wait` 5 s, then `wait` 20 s. The gate released at the
  28 s margin (`budget_exhausted`), and the Rules granted entry.
- The verifier measured 28.4 s of judge-caused waiting up to the received
  release, within the 30 s budget.
- All five verifiers passed, and `/status` reported `completed`.
- No simulator container remained after the Gateway stopped.

## Judge A/B on recorded returns (`scripts/evaluate_yokohama_pad_judge.py`)

The reentry bundles hold real pad requests, with camera advisory receipts, and
captured lead trajectories for three departing and three returning cases. The
test points are the 133 requests where the Rules and the existing advisory
allowed entry. Hindsight truth is whether the lead re-entered the 6 m radius
within 60 s; no arm sees it. Each cell counts the waits at those points.

| Arm | Return, approach visible (32) | Return, lead still holding (24) | No return (77) |
|---|---|---|---|
| Rules only | 0 | 0 | 0 |
| Kinematic rule: inward speed > 0.2 m/s | 32 | 0 | 0 |
| DeepSeek v1 (flown 2026-09-30) | 32 | 22 | 76 |
| DeepSeek v2 (computed facts) | 32 | 2 | 0 |

All 266 DeepSeek answers were valid.

- **v1 waited almost everywhere.** Its rationales misread raw distances against
  the radius.
- **v2 matches a simple rule.** It waits only on approach. Its two extra waits
  are at the onset of a return, at -0.12 and -0.15 m/s. Its decisions match a
  0.1 m/s inward-speed rule in 132 of 133 points. With these facts, the judge
  does not add information beyond a one-line kinematic rule; it adds an
  accurate operator explanation.
- **Cost that remains.** Each Rules entry still waits for the call, about
  5–10 s.
- **Unknowable cases.** A return that starts after the decision is not
  knowable by any arm.

**Chat flight with v2** (2026-09-30, fixture city models; task
`yokohama_d00df180748e4602`)

- The lead was 6.8 m outside the radius, moving away at 0.95 m/s. DeepSeek
  answered `enter`, with a factually correct rationale.
- Judge-caused waiting was 2.5 s, which was the call itself. The whole pad
  wait was 36.0 s, the same as the Rules-only native run.
- All five verifiers passed.

## Limits

- Only one route and destination exist. The planner cannot add either.
- GPU provisioning for native city models is outside the Gateway. It is a
  separately approved, cost-capped session.
- Chat shows the 2D track and text. The 3D scene is visible only in the
  simulator and recorded images.
- A pad-judge answer is a proposal to wait, not an approval or a dispatch.
  Entry always comes from the Rules.
- A Gateway restart during a flight leaves the task `needs_attention`. The
  outcome is not resumed or claimed.
