# Native VLA and WAM in one flight

Both explicit loopback endpoints select a sequential native integration:

```sh
missionos ship-delivery run-sitl \
  --scenario examples/fixture_missions/ship_delivery/urban-compact.json \
  --urban-case static_center --urban-policy onboard_anwm_static \
  --aerovla-url http://127.0.0.1:18117 --anwm-url http://127.0.0.1:18118 \
  --approve-sitl --timeout-seconds 900 --output-dir /tmp/new-native-joint-flight
```

The stationary cases and existing absolute bounds remain unchanged. AeroVLA
uses a fresh paired image and completes one guarded maneuver with observed
settling and LOITER handoff. Only then does the collector start a new 4 Hz RGBD
history. ANWM predicts candidate views, MissionOS proposes a route, and fresh
observations constrain dispatch. Delivery and recovery retain their independent
contact, stability and trajectory checks. Neither model grants approval.

The downward camera runs at 20 Hz when capture and the VLA executor coexist,
preserving exact timestamp joins for 10 Hz VLA images and 4 Hz WAM inputs.
It does not interpolate missing frames. Existing standalone modes retain their
original sensor rates. The first eight capture bundles are retained startup
data, followed by twenty uninterrupted bundles, with only sixteen sent to WAM.

The integration verifier requires both independently verified native chains,
one run/world/plan, a strictly ordered handoff/capture/decision/dispatch sequence,
and WAM sensor timestamps after the post-VLA LOITER observation. A fixture or
logged unused forecast cannot satisfy the combined proof. Verification of this
chain is separate from whole-mission completion.

## One-GPU operation

Start ANWM with `--cpu-between-requests` to keep its weights in host memory
between requests. Its transfer to CUDA, actual prediction and return to CPU
all count toward the existing 75-second host exchange limit. Exceptions also
release the model allocation. Model precision, weights, seed and diffusion
steps remain unchanged.

Start AeroVLA with `--exit-after-request`. After its one successful response is
flushed, the process exits and releases CUDA memory. Before each new flight,
start a new service, complete its synthetic warmup and bind its new identity in
the approved mission plan. No service is restarted during a flight to replace
a rejected output. Both options are explicit and default off.

The optional AeroVLA `--constrain-action-format` flag masks next-token choices
to the native three-bin syntax (each bin 0–98) or a `LAND` proposal. It uses
Transformers' `prefix_allowed_tokens_fn` during the same single generation;
there is no output rewriting, numeric substitution or retry. All bins, including
terminal bins and explicit `LAND`, remain available. Existing terminal, motion,
freshness and clearance guards still reject unsuitable proposals. The default
remains unconstrained greedy decoding. The selected decoding policy is included
in the pinned service identity and must be frozen as part of each qualification.

This addresses malformed text generation, not the semantic quality of the
chosen motion. The saved `故4924 49</s>` response remains a rejected attempt.
See the [generation API](https://huggingface.co/docs/transformers/v4.42.4/main_classes/text_generation)
for the prefix constraint hook.

For an urban-entry inspection that must hold altitude, the separate opt-in
`--inspection-level-flight` flag requires the format flag and narrows the
vertical output to native bins 47–51 (about ±0.204 m). Forward and yaw bins
remain 0–98; `LAND` and terminal bins remain possible and remain subject to
the independent terminal guard. The policy is `aerovla_inspection_grammar.v1`,
with the selected range pinned in the service identity and approved plan.
This is an explicit mission-phase action-space restriction, not a claim of
unrestricted three-dimensional VLA competence. It does not rewrite a response,
choose a particular motion, relax the altitude envelope or replace clearance
and observed-completion verification. It should not be used for a descent stage.

The preceding format-only cohort proposed `55 84 49</s>` (3.57 m down) at urban
entry and was correctly rejected by the altitude envelope. That failure remains
recorded separately; a level-flight cohort changes the model's allowed action
space and must not be pooled with unrestricted or format-only attempts.

## Qualification and scope

Freeze the source, service configuration, model identities, three stationary
cases and numeric bounds before paid flight trials. Require all three 100 m
offshore cases to complete within 900 wall seconds, followed by a separately
frozen 1,000 m offshore extension with the same 900-second bound. Preserve all
attempts and reject failure-driven model retries. Rules superiority is not a
gate. The VLA age limit remains two seconds, target error 0.25 m and altitude
error 0.15 m; the [WAM absolute bounds](ship-anwm-static.md) remain unchanged.

This qualifies sequential participation in one stationary-ship simulator
mission. It does not establish continuous joint learned control, VLA-generated
whole routes, moving-obstacle forecasts, deployable odometry, real strong-wind
performance, moving decks, ten-aircraft coordination or physical delivery.
Current sensor/material assumptions and simulator-based VLA clearance remain
explicit. Cloud startup requires the existing user budget and opt-in approval;
the CLI never provisions compute.

## Recorded qualification

The [same-flight report](../examples/ship-native-joint-report/REPORT-ja.md)
retains six implementation stages: original, target association, ACK ordering,
action syntax, level inspection, and canonical numeric permits. Each has its
own frozen source revision and attempt denominator. The final stage is one
predeclared targeted 1,000 m follow-up within the remaining compute budget.
It does not establish all four conditions on the final implementation; earlier
100 m passes and failed integration certificates are not pooled or relabeled. The public replay exports reviewed synthetic
images and recorded positions; raw cloud logs remain private. Public hashes
identify those records but do not replace a full independently runnable archive.

For the 1,000 m extension, use the same native command with
`--scenario examples/fixture_missions/ship_delivery/clear.json` and
`--urban-case static_center`. The empty scenario selects the default 1,000 m
offshore distance and 200 m urban segment. The three shorter cases instead use
`urban-compact.json` with `static_center`, `static_near` and `static_clear`.
Output directories must be new, and both native services must have completed
their required setup and warmup before the opt-in command is run.
