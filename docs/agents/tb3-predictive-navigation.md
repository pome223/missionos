# TB3 predictive recovery: maintainer contract

The [detailed research report](tb3-observed-recovery-research-report.md)
adds historical comparisons, full trial analysis, observed trajectories, latency
and usage accounting, and a critical audit of Agent reasoning.

## Scope and authority

`missionos navigation run tb3` is an opt-in local Gazebo Garden / ROS2 Humble /
Nav2 path. It imports the existing Mission Assurance graph and PolicyStore.
It does not change Gateway dispatch or the general incident graph's advisory
`continue` behavior. Only the dedicated validated navigation actions map to
`hold` and `continue` for graph assessment.

The fixed original goal is `(1.0, 0.0)`; the optional detour is
`(0.35, -0.70)` then `(1.0, 0.0)`. Maximum requested speed remains 0.20 m/s.
No additional controller speed or tolerance tuning is included.

| Operator flag | Approved scope | Required runtime checks |
| --- | --- | --- |
| `--run-sim` | Start this owned local simulator invocation | New output directory and owned-container cleanup |
| `--approve-bounded-wait` | One stationary wait, at most 5 s, then original goal | Prediction-grounded Recovery, Assurance, Rules, actual clearance after waiting, then Assurance continuation |
| `--approve-bounded-detour` | One fixed two-goal detour after observed wait failure | Fresh failed-wait evidence, Recovery, Assurance graph, separate Rules budget, route planning before and after Agent processing |
| `--approve-observed-continue` | One original-goal continuation without a wait | Fresh observed clearance, Recovery, Assurance graph, separate Rules budget, planning and observation revalidation before dispatch |

The two optional grants default off and require the Agent policy and bounded
wait grant. An initial Assurance `continue` answer requests observation only;
it cannot consume a wait grant to dispatch. A successful observed continuation
has zero executed wait and no wait reservation. Budgets are one-use and the
alternative dispatch grants cannot be replayed or combined into extra goals.

## Evidence and expiration

Prediction records include observation and target simulator times. Camera
history has four frames at 0.5 s intervals; the adapted head predicts a mask
four seconds ahead under a hold action. No scenario label, obstacle speed,
future ground-truth image, or hidden actor pose is an inference input.
The simulator and actor continue advancing during Agent processing and injected
test delays. An expired prediction triggers reobservation rather than reuse.

Fresh actual observations bind the camera timestamp, image SHA-256, exposure,
stationarity, and verification result into a canonical observation reference.
Recovery must return that reference; Rules checks it against the supplied
evidence. Fresh Nav2 planning is required before dispatch. A 2 s simulator-time
dispatch deadline bounds the interval after Agent processing. A camera callback
may wait up to 0.25 wall seconds for the clock to catch up when it is less than
0.05 simulator seconds ahead; timestamps and freshness limits are not rewritten.

The central red-mask exposure threshold is 0.06. A clear mask is a scene-specific
obstruction test, not a complete obstacle detector. PNG-file hashes and raw-camera RGB hashes identify different byte
representations and must not be compared as if their difference proved a
different frame. The report
confirmed that decoding the verification PNG reproduced the observation RGB
hash in both detour runs and the observed-continuation run. The Agent
misinterpreted this distinction in one explanation. The verification PNG and
its timestamp must match the actual observation used by Rules.

Recovery text that explicitly claims collision freedom or safety is rejected
by the bounded language check before Rules. This catches the tested phrases;
it is not a general semantic guarantee. A successful computed route explicitly
has `collision_free_claimed=false`.

Completion requires native Nav2 success, observed robot motion, observed
endpoint error within 0.30 m, the appropriate authority chain, unchanged source
and evidence hashes, and verified owned-container removal. With incoming detour
heading, each intermediate goal is also checked within 0.30 m. This preserves
the legacy controller's observed operating range, not an exact waypoint claim.
The final goal retains yaw zero. The outgoing heading remains the default;
the measured cohort explicitly opted into incoming heading.

`navigation status` checks the saved evidence hashes and re-evaluates completion
from the referenced results. It does not repeat a live run. A deliberately
denied action is retained as CLI status `failed` with exit 1 and its stop reason;
it is never counted as arrival. Failed runs also retain cleanup status.

## Setup

From a clean checkout, create a host environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]' \
  -e packages/missionos-core -e packages/missionos-cli -e packages/missionos-gateway
. .venv/bin/activate
```

The model-free `image-history` policy requires Docker and the TB3 image. A
standalone build recipe is included:

```sh
docker build -f scripts/tb3_prediction/Dockerfile.standalone \
  -t missionos-tb3-prediction:garden-repair .
```

The recorded trial reused an existing local Garden image; a fresh image build
was not validated in that cohort. ROS-Gazebo and TurtleBot3 source revisions
are pinned in the Dockerfile, while package repositories are external inputs.
The normal test suite runs without Docker, Agent credentials, or model assets.

The runnable [history config](../../examples/navigation/tb3-history.json)
resolves paths relative to its own directory. Preview omits `--run-sim` and
does not start containers. Every execution needs a new path below the configured
repository's `output/` directory. Raw receipts, images, configurations, provider
responses, and logs stay there and are not public documentation assets.

## Optional NWM and live Agent assets

`--policy nwm` retains the existing deterministic prediction-based decision rule.
`--policy agent-nwm` sends bounded evidence to live Assurance and Recovery.
Neither command downloads or trains a model. A task-adapted head is required;
the stock NWM checkpoint alone cannot reproduce these results.

Provide a local JSON config, for example at `output/local/agent-config.json`:

```json
{
  "repository": "../..",
  "python": "/path/to/nwm-mps-environment/bin/python",
  "agent_python": "../../.venv/bin/python",
  "bundle": "/path/to/local/nwm-bundle.json",
  "tb3_manifest": "/path/to/local/future-mask-manifest.json",
  "tb3_actor_speed_mps": 0.16,
  "tb3_actor_initial_y_m": -0.80,
  "tb3_controller_profile": "legacy",
  "tb3_via_heading": "incoming",
  "test_agent_first_plan_delay_wall_s": 0
}
```

The NWM Python environment needs Apple Silicon MPS, PyTorch, diffusers,
NumPy, Pillow, and the pinned upstream implementation's dependencies. The
current adapter requires MPS; CUDA and CPU inference were not qualified here.
The Agent Python environment is the installed MissionOS environment. Select
the supported provider with `MISSIONOS_LLM_BACKEND=deepseek` and supply
`DEEPSEEK_API_KEY` only in the authorized host environment. Do not put its value
in configs, logs, committed files, or the simulator container. `agent_env_file`
is an optional host-local alternative. The recorded live Agent used
`deepseek-v4-flash`.

Required external asset structure:

| File | Required fields / checks |
| --- | --- |
| Bundle JSON | `upstream_directory`, `vae_directory`, relative to the bundle; clean pinned upstream revision and exact VAE hashes |
| Initial adaptation manifest | `status=complete`, `base_sha256`, `checkpoint_file`, `checkpoint_sha256`; checkpoint is a sibling file and its `ema` state loads strictly |
| Future-mask manifest | `status=complete`, `head_kind=NWM_CDiT_future_obstacle_mask_hold_4s`, `context_period_s=0.5`, `base_sha256`, `initial_adaptation_manifest`, `initial_adaptation_manifest_sha256`, `checkpoint_file`, `checkpoint_sha256`; optional `input_motion_scale` defaults to 1.0 |

Manifest references are relative to their manifest. The task checkpoint and
parent checkpoint each have checked hashes; the parent manifest hash is also
checked. The upstream revision is
`3f6cd8e70d6f2d1e2b9684acff510710135f0f41`; exact base/VAE hashes are in
[`nwm_assets.py`](../../scripts/tb3_prediction/nwm_assets.py).
This is an inference/authority integration, not a public training recipe or
redistribution of the experimental weights. Fixture tests and the history
policy can be run without those assets; reproducing the learned result needs
a compatible externally supplied adaptation.

External model assets retain their own terms. See the official
[NWM repository](https://github.com/facebookresearch/nwm),
[model access page](https://huggingface.co/facebook/nwm), and
[CC BY-NC 4.0 license](https://github.com/facebookresearch/nwm/blob/main/LICENSE.md).
The MissionOS license does not grant additional rights to those assets.

## Public-source verification

The seven-run protocol was fixed before execution on 2026-09-15. Source and
model hashes were checked throughout, with no prompt/source edits, replacement
trials, or relabeling based on the desired answer. Actual observation controls
the action even if it differs from the scenario's expected class. The
[sanitized result record](evidence/tb3-observed-recovery-20260915.json) contains
all seven terminal outcomes and the public runtime source hashes; raw local
artifacts and private configuration paths are not included.

All trials use the same wide-box scene, legacy controller, incoming detour
heading, fixed goal, and adapted model. Each host invocation was bounded to
420 wall seconds and further trials were prohibited after unverified cleanup.
Main acceptance: observed arrival after recovery, observation-grounded decisions,
and approved action bounds. Elapsed time is secondary, not a speedup gate.

| Run | Speed m/s / initial y m | First-call delay (wall s) | Detour / continue grants | Observed result | Goals | Decision-to-end (sim s) |
| --- | --- | --- | --- | --- | --- | --- |
| `wait-direct-r1` | 0.29 / -0.88 | 0 | no / no | Wait, actual clear, arrived | 1 | 16.502 |
| `blocked-no-detour` | 0.16 / -0.80 | 0 | no / no | Stopped, no detour grant | 0 | 3.724 |
| `blocked-recovery-r1` | 0.16 / -0.80 | 0 | yes / yes | Wait still blocked, detour, arrived | 2 | 34.658 |
| `expired-no-continue` | 0.18 / -0.95 | 8 | yes / no | Expired, reobserved, stopped; no continue grant | 0 | 7.102 |
| `expired-continue` | 0.18 / -0.95 | 8 | yes / yes | Expired, reobserved clear, arrived | 1 | 23.580 |
| `blocked-recovery-r2` | 0.18 / -0.95 | 0 | yes / yes | Wait still blocked, detour, arrived | 2 | 32.108 |
| `wait-direct-r2` | 0.30 / -0.95 | 0 | no / no | Wait, actual clear, arrived | 1 | 16.792 |

Both intended wait/direct runs arrived; both failed-wait recovery runs arrived;
the separately approved expiry continuation arrived. The two missing-grant
controls stopped with zero goal requests. All seven runs verified cleanup.
This is five observed arrivals plus two intended authority stops, not a broad
success-rate estimate.

The `blocked-no-detour` / `blocked-recovery-r1` pair fixes obstacle speed and
start position while changing authority. Both observe a failed wait; the
second can take the separately approved recovery route. Live inference timing
means their actual images are not byte-identical. The expiry pair fixes an
8 s first-Agent-call wall delay: both detect stale prediction and reobserve;
only the separately approved continuation may dispatch. These are bounded
operational comparisons, not a statistical estimate of model superiority.

The predictor's manifest hash in the cohort is
`c0267966223a3d673d48224fb20eec59708f23f6ce3e42ab2f75e562ad422825`.
The frozen local protocol hash is
`79911575a407a59801f174b39b31e3f039eb1768be81a19fc24765dc8557fd25`.
Hashes identify the measured inputs; they do not make unpublished assets
available or substitute for runtime verification.

### Exact CLI boundary

The public source was installed into an independent `.venv-public` environment.
For each row, a local config named `<run>-config.json` supplied that row's speed,
initial y, and first-call delay. The executed command was:

```sh
.venv-public/bin/missionos navigation run tb3 --policy agent-nwm \
  --config output/public-tb3-20260915/frozen/<run>-config.json \
  --output output/public-tb3-20260915/frozen/<run> \
  --run-sim --approve-bounded-wait
```

Append `--approve-bounded-detour` where the table says detour approval, and
`--approve-observed-continue` where it says continuation approval. These flags
are operator grants, not Agent-generated permissions. Then every completed or
stopped run was inspected through the installed entrypoint:

```sh
.venv-public/bin/missionos navigation status output/public-tb3-20260915/frozen/<run>
```

The affected boundary is installed CLI → host predictor and live Agent →
Assurance graph / PolicyStore / Rules → local simulator Executor → native
Nav2 goals and actual observations → CLI completion validation / owned cleanup.
This does not cover Gateway chat, a remote deployment, physical hardware,
contact-free motion, unseen scenes, or PX4.

A separate model-free smoke ran the same installed CLI with
`--policy image-history --config output/public-tb3-20260915/history-config.json
--output output/public-tb3-20260915/history-smoke --run-sim`. Its config fixed
speed 0.29 m/s, initial y -0.88 m, legacy controller, incoming heading, and
the public host Python. It selected a detour and reached the goal in 44.122
simulator seconds, with `learned_wam_invoked=false` and verified cleanup.
The installed `navigation status` revalidated that result. A separate preview
without `--run-sim` created no output directory and invoked no runtime.
This smoke validates the usable model-free entrypoint; it is outside the
seven-run Agent cohort and is not a paired speed comparison.

### Automated checks

```sh
.venv-public/bin/python -m pytest -q
.venv-public/bin/python scripts/check_v0_1_0_stable_gate.py --pretty
.venv-public/bin/python scripts/check_v0_2_0_stable_gate.py --pretty
.venv-public/bin/python scripts/check_smoke_inventory.py --pretty
```

The public full suite passed 2,668 tests. Contract cases cover stale evidence,
wrong observation references, separate grant consumption, replay rejection,
goal bounds, missing planning evidence, and completion/evidence tampering.
Those tests supplement the live cohort; they do not themselves prove physical
behavior or provider reliability.
