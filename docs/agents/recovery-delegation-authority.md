# Recovery Delegation Authority

Agent-facing reference for the recovery-delegation surfaces: two-phase
reflex/deliberation split, perception claims, shadow measurement, and
action promotion. Read `docs/concepts/recovery-delegation.md` first for the
plain-language model; this page is the field/env/module map.

## Env Gates

Every surface below is opt-in. Nothing here fires unless the relevant env
var is explicitly set.

| Surface | Env var | Default when unset |
| --- | --- | --- |
| TB3 camera perception capture | `MISSIONOS_TURTLEBOT3_CAMERA_PERCEPTION_ENABLED` | disabled, no capture attempted |
| TB3 promoted-action envelope override | `MISSIONOS_TURTLEBOT3_PROMOTED_ACTIONS_JSON` | no file consulted, envelope unchanged |
| PX4 reflex RTL dispatch | `MISSIONOS_PX4_REFLEX_RTL_ENABLED` | disabled, budget exhaustion is recorded but nothing is dispatched |
| PX4 reflex RTL endpoint | `MISSIONOS_PX4_REFLEX_RTL_ENDPOINT_PORT` | required when RTL is enabled |
| PX4 reflex reserve margin | `MISSIONOS_PX4_REFLEX_RESERVE_LANDING_PERCENT` | module default reserve |
| ROS2/Nav2 bridge (all bridge actions incl. `cancel_goal`, `capture_camera_frame`) | `ROS2_NAV2_BRIDGE_COMMAND` | `Ros2Nav2BridgeError` — no command configured |
| Bounded bridge dispatch smoke path | `RUN_MISSIONOS_ROS2_NAV2_BOUNDED_DISPATCH_SMOKE` | disabled |
| Cancel-goal stop observation window | `ROS2_NAV2_CANCEL_STOP_OBSERVE_S` | 2.0s |

## Modules

- `src/runtime/px4_recovery_reflex.py` — `build_px4_recovery_reflex` computes
  `deliberation_budget_seconds` from `(battery_percent -
  reserve_landing_percent) / discharge_pct_per_minute`, capped at
  `MAX_DELIBERATION_BUDGET_SECONDS` (180s). Pure computation, no dispatch.
- `src/runtime/px4_recovery_reflex_dispatch.py` — `dispatch_px4_recovery_reflex_rtl`
  performs the bounded MAVLink RTL over loopback UDP;
  `default_px4_reflex_dispatcher_from_env` wires it from the env gates above;
  `watch_px4_recovery_reflex_from_battery` dispatches at most once per
  exhaustion event. `operator_approval_performed` is always `False` on this
  path — see "Reflex Authority" below.
- `src/runtime/perception_claim.py` — `build_perception_claim` /
  `build_perception_claim_from_camera_observation` construct the
  `missionos_perception_claim.v1` artifact; `guard_perception_claim_support`
  enforces the conservative/progressive asymmetry.
- `src/runtime/mission_autonomy_envelope.py` — `CONSERVATIVE_RECOVERY_ACTIONS`
  is the set a claim may support even when uncorroborated.
- `src/runtime/recovery_shadow_ledger.py` — `collect_recovery_shadow_comparisons`
  is read-only: it walks the task store and extracts
  `missionos_turtlebot3_recovery_shadow_comparison.v1` records in
  chronological order. Never writes, never grants authority.
- `src/runtime/recovery_action_promotion.py` —
  `evaluate_recovery_action_promotion_candidates` proposes (evidence only,
  never mutates); `apply_recovery_action_promotion` is the only function in
  the module that mutates a live envelope, and only when given a non-empty
  `operator_approval_ref` against an envelope that is already
  operator-approved.
- `scripts/turtlebot3_recovery_promotion_cli.py` — operator entry point,
  `evaluate` (read-only) and `apply` (requires
  `--operator-approval-ref`) subcommands.
- `src/runtime/turtlebot3_home_mission.py` — `_dispatch_harness_stop` wires
  the TB3 reflex into the ROS2/Nav2 bridge's `cancel_goal`;
  `_capture_camera_perception_observation` wires the perception-claim loop
  into obstacle recovery; on a capture failure it records a blocked-status
  artifact and lets the mission continue instead of raising — no perception
  claim is produced from a failed capture, and no execution authority is
  granted either way; `_build_autonomy_envelope` loads the promoted-actions
  file when the env var is set.

## Artifact Schema Versions

- `missionos_px4_recovery_reflex.v1`
- `missionos_perception_claim.v1`
- `missionos_recovery_shadow_ledger.v1`
- `missionos_turtlebot3_recovery_shadow_comparison.v1`
- `missionos_turtlebot3_recovery_reflex.v1`
- `missionos_turtlebot3_camera_perception_pipeline.v1`
- `missionos_turtlebot3_harness_stop_dispatch.v1`

## Authority Boundary

Follow the field dictionary in `docs/agents/claim-semantics.md`. Two rules
specific to recovery delegation, both added after review found real bypass
paths — do not regress either:

### ACK is not stop confirmation

The ROS2/Nav2 bridge's `cancel_goal` response and
`_dispatch_harness_stop`'s resulting artifact carry three distinct booleans,
not one:

- `cancel_accepted` — the navigation stack acknowledged the cancel request.
- `stop_observed` — post-cancel odometry was independently sampled and shows
  the robot stopped (delta under the motion threshold within the
  observation window).
- `stop_confirmed` — `cancel_accepted and stop_observed`. Only this field
  means the robot is actually known to have stopped.

An ACK alone (`cancel_accepted=True`, `stop_observed=False`) must never be
treated as `stop_confirmed`. This mirrors the general ACK-vs-runtime-progress
split in `docs/agents/claim-semantics.md`; recovery delegation just gives it
concrete field names for the stop case.

### The perception-claim guard runs on context, not citations

`guard_perception_claim_support` and
`guard_turtlebot3_recovery_planner_output` evaluate **every** perception
claim present in context, whether or not the proposal cites it — an earlier
version only checked cited claims, which meant a proposal could rely on an
uncorroborated camera claim for a progressive action simply by omitting the
citation field. Two enforced rules now:

1. If perception claims were provided at all, `cited_perception_claim_ids`
   is required; its absence is itself a blocking reason
   (`cited_perception_claim_ids_required_when_claims_present`).
2. Any uncorroborated claim present in context — cited or not — blocks a
   progressive `selected_action`
   (`uncorroborated_perception_claim_in_context_requires_conservative_action:<claim_id>`).

Corroboration signals also carry a `corroboration_binding` metadata block
(`temporal` / `spatial` / `target_identity`) stating exactly what was
verified. The current implementation binds `temporal:
same_segment_bridge_receipt` only; `spatial` and `target_identity` are
`unbound`. Do not describe a corroborated claim as spatially or
identity-confirmed until that binding is actually implemented.

Open question, not yet decided: whether corroboration should be tightened
to conservative-actions-only until full spatio-temporal (timestamp, pose,
region) binding of camera-frame and costmap evidence exists, or whether the
current same-segment-window binding is sufficient to support progressive
actions. Anyone changing this guard should resolve that question first,
not assume the current behavior is final.

## Reflex Authority Is a Separate Category

`dispatch_px4_recovery_reflex_rtl` intentionally does not require
`operator_approval_performed=True`. Within the set of dispatch paths this
recovery-delegation contract covers, every other path pins
`operator_approval_performed`/`operator_approval_ref` as a required,
non-empty invariant before dispatch; this is the one path in that set that
does not. This is a deliberate, narrowly-scoped exception:

- It is bounded to one action (return-to-launch).
- It fires at most once per budget-exhaustion event
  (`watch_px4_recovery_reflex_from_battery`).
- It only exists to prevent a worse outcome (running out of deliberation
  time with no fallback) while a human approval is pending.

Do not extend this pattern to other dispatch paths, and do not relax the
`Literal[True]` operator-approval invariant on any other adapter or planner
model by analogy to this one.

## Verifier Contracts Touched

Recovery delegation does not introduce a new verifier; it produces evidence
that existing verifier and claim-normalization code must handle correctly:

- `runtime_claim_evidence.py`'s two-phase artifact/runtime split
  (`docs/agents/claim-semantics.md`) applies unchanged to recovery-delegation
  claims — an artifact-only `stop_confirmed` or `physical_execution_invoked`
  is not runtime truth without `runtime_invocation_evidence`.
- `hardware_adapter_contract.py`'s `physical_execution_invoked` pin remains
  `Literal[False]` on every recovery-delegation model; the PX4 reflex RTL
  path uses the same opt-in real-serial exception as the rest of the
  codebase, not a new one.
- Perception-claim artifacts are `evidence_only=True`,
  `approval_created=False`, `dispatch_authority_created=False`,
  `physical_execution_invoked=False` — a claim can block a progressive
  action but can never itself create authority to act.

## Test Coverage Pointers

- `tests/contract/test_perception_claim.py` — corroboration asymmetry,
  citation-bypass regression, unknown-claim rejection.
- `tests/contract/test_px4_recovery_reflex_dispatch.py` — budgeted RTL
  dispatch and ACK observation.
- `tests/contract/test_recovery_shadow_ledger.py` — chronological,
  de-duplicated shadow-comparison extraction.
- `tests/contract/test_ros2_nav2_dispatch_bridge.py`,
  `test_ros2_nav2_turtlebot4_camera_frame.py` — bridge `cancel_goal` /
  `capture_camera_frame` boundary and fail-open capture behavior.
- `tests/contract/test_turtlebot3_home_mission.py` —
  `test_harness_stop_ack_without_stop_observation_is_not_confirmed` is the
  direct regression test for the ACK-vs-stop-confirmation rule above.

## Where To Go Next

- `docs/concepts/recovery-delegation.md` for the plain-language model.
- `docs/agents/claim-semantics.md` for the general claim-boundary field
  dictionary this extends.
- `docs/agents/hardware-adapter-contract.md` for the adapter-level
  `physical_execution_invoked` and ACK-status contract this composes with.
