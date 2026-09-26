# Native VLA proposal constraint adapter

`src/runtime/ship_vla_adapter.py` converts AeroVLA bins into a nominal NED
candidate inside a host-supplied envelope. It has no approval or transport
method. `candidate_eligible=true` means candidate-only; `approval_granted`,
`dispatch_allowed` and `dispatch_invoked` always remain false.

## Contract

The host supplies run/world/plan, frame/clock identities, phase, expiry and
spatial/kinematic limits. The model supplies only text and its observation
binding. Reject extra authority fields, malformed/nonfinite state, mismatched
identities and terminal/LAND outputs. Never clamp or repair model proposals.

Pinned AeroVLA controller `2c5ae0987a484ab92f00dd9d9ed493cb3e98e492` uses
bins `[0,98]`: forward `[0,5]` m, down `[-5,5]` m, yaw `[-1.1,1.1]` rad.
Positive down decreases altitude. It turns before translating; absolute yaw
>= 0.25 rad suppresses horizontal motion. Heading follows its extrinsic `zyx`
Euler extraction, which differs from conventional PX4 yaw under roll/pitch.
A future executor must validate this convention and observed tracking error.

Nominal duration includes a 3 s rotation timeout, translation at 1 m/s (at
least 1 s for small nonzero displacement) or vertical positioning at 2 m/s,
and 0.5 s braking. Upstream vertical positioning also has a 3 s timeout.
This is not verified PX4 duration or arrival. The spatial check covers a
nominal straight segment, inflated by vehicle margin and observed pose drift;
it does not establish obstacle clearance, tracking accuracy or safe descent.

Input/current observations must share identities, phase and estimator epoch,
remain fresh, and show armed PX4 AUTO_LOITER with low speed. Check position and
heading drift across inference. Receipts bind both states, proposal and envelope.
State and limits are trusted host inputs; this in-process API does not
cryptographically authenticate its caller. Scope remains simulation-only.

## Runtime verification

```sh
missionos ship-delivery run-sitl \
  --scenario examples/fixture_missions/ship_delivery/urban-compact.json \
  --urban-case brake_stop --urban-policy onboard_stopping \
  --vla-guard-smoke --approve-sitl --output-dir /tmp/new-vla-guard-run
```

Freeze limits before plan hashing: entry +/-10 m north/east, cruise altitude
+/-1 m, vehicle margin 0.5 m. After observed loiter, inspect six explicit test
vectors: bounded motion, altitude violation, stale input, wrong plan, outside
corridor and terminal output. Stale/corridor cases inject declared faults into
copies of measured state. These are guard tests, not native model inference.

Bind RGB and raw PX4 position/attitude/status, timestamps and reset counters.
The verifier reopens image/deployed source hashes, checks sample/event timing,
and recomputes every result. Missing or corrupt receipts fail verification.
A smoke failure stops before urban dispatch. A pass allows the existing rule
route to continue; no native VLA candidate is sent to PX4.

The 2026-09-25 frozen smoke at implementation commit `4794fd4` passed on its
first trial: six guard checks, one candidate-only result and five reason-specific
rejections. The compact `brake_stop` rule mission delivered and returned; saved
artifacts passed independent rereading, and its container was removed. Related
contract tests: 277 passed. This establishes the guard/worker/verifier boundary,
not native-model flight, moving-ship recovery, ten-drone operation or a 1 km leg.

## Archived native inference

```sh
missionos ship-delivery vla-audit \
  --run-dir /path/to/captured-run --model-input /path/to/aerovla-request \
  --model-output /path/to/aerovla-result --output /tmp/vla-audit.json
```

Reproduce preparation from raw camera assets, compare pixels/model mosaic and
check request/runtime/model identities. Reparse raw output text, ignoring
convenience proposal fields. Restore the recorded native runner before audit;
changed preprocessing must not silently reinterpret evidence. File bindings
cannot independently rerun or attest inference.

Historical requests use Gazebo geometry and lack fresh PX4 hold/reset evidence.
The new diagnostic envelope was not in their original approval. Report missing
state explicitly; velocity/reset placeholders cannot pass the hold gate. Force
`archived=True`, disable authority, preserve historical timestamps. All archived
assessments are ineligible even if their geometry fits. No model is rerun.

The next boundary requires an approved PX4 controller contract, fresh obstacle
clearance, revalidation at dispatch and observed completion. Guard success or
rule-driven delivery alone does not establish an executed native VLA/WAM path.
Step 2 uses [absolute qualification](ship-onboard-step2.md), without requiring
superiority over ordinary rules.

The separate [PX4 executor qualification](ship-vla-executor.md) exercises that
boundary with an explicit fixture, retaining the native-model claim limit.
