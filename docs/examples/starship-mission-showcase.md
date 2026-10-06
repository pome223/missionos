# Starship-inspired simulation through MissionOS

MissionOS connects an opt-in spacecraft simulation to its ordinary approval
and evidence loop. The simulator integrates position, velocity, quaternion,
angular rate, fuel, and finite engine/gimbal/control-surface states. Those
recorded states drive the synchronized 2D/3D replay; the AI does not set the
vehicle's pose or act as a flight controller.

The demonstration is numerical development software. It does not establish
SpaceX vehicle fidelity, physical spacecraft execution, Starlink service,
successful launch-derived tower catch, or an improvement from using an LLM.

## Watch the recorded simulations

The [offline replay and media bundle](../assets/starship-mission-20261006/index.html)
contains three separately labelled records:

| Record | What it shows | Limit |
| --- | --- | --- |
| [Nominal flight](../assets/starship-mission-20261006/nominal-flight.mp4) | Launch, separation, 26 rigid payload releases, and Ship return | Low-speed hull contact is not validated landing; the booster did not reach tower catch |
| [Deployment supervision](../assets/starship-mission-20261006/deployment-supervision.mp4) | A recorded Jev route, fixed skip procedure, Rules, and later measured sequencer states | DeepSeek was not called; no payload separated, and the mission did not complete |
| [Negative return development](../assets/starship-mission-20261006/return-negative.mp4) | A 60-second continuation from a saved launch-derived booster state | No handoff; final fuel 3.569 t was below the 6.918 t support reserve |

These videos replay saved simulator records, not a new flight or a live GUI
recording. Their playback rates and selected fields are documented with the
media. Reviewed derived display data is included; private run stores, approval
keys, task databases, raw provider exchanges, and full private output are not.
The short fixture below does not regenerate these historical full-flight clips.
GitHub displays HTML source rather than running the replay. Download the whole
media bundle, or serve the checkout with
`python -m http.server 8000 --bind 127.0.0.1` and open
`http://127.0.0.1:8000/docs/assets/starship-mission-20261006/index.html`.

## Try the bounded public fixture

Install the repository and CLI packages using the [Chat Quickstart](../../README.md#chat-quickstart).
Add the CPU simulation dependencies (this does not install native Basilisk):

```sh
python -m pip install -e '.[spaceflight]'
```

Start a fresh fixture Gateway in one terminal:

```sh
python scripts/start_starship_gateway.py --fixture-planner \
  --state-dir output/starship-public-fixture-state --port 18794
```

In another terminal, connect the normal CLI:

```sh
missionos --gateway-url http://127.0.0.1:18794 \
  --state-path output/starship-public-cli-state.json \
  chat --session-id starship-public-demo
```

Ask for `Starship sixdof_gimbal_step`. Inspect the fixed 30-second numerical
test and its source/profile bindings, then enter `/approve`, `/run`, and
`/status`. Approval and execution are separate steps. `yes` or `OK` is not an
approval. The fixture planner uses deterministic catalog matching, not LLM
inference, and makes no provider call.

The same Gateway serves the operator console at
`http://127.0.0.1:18794/missionos/starship/operator`. It keeps plan, approval,
execution, verification, and replay controls separate. The returned `verified`
status means saved-record integrity passed; it does not mean successful return,
capture, mission completion, or authenticated human identity.

Stop the Gateway after the example. Use fresh state/output directories for
another attempt so earlier failed or incomplete evidence is preserved.

## What the operating loop verifies

The regular route binds a plan to its software and profile, consumes approval
once, starts a separate credential-free worker, and checks its signed receipt
and served artifact hashes. Unapproved, stale, source-changed, duplicate, and
cross-session requests are rejected.

The longer `sixdof_deployment_supervised` scenario injects an explicit synthetic
fault: a release command is accepted without a separated body. Fresh reports
feed a bounded proposal. Child-side Rules check its current evidence and scope
before one `skip_remaining_deployment` operation. Two later observations must
show the sequencer change. Acceptance alone is insufficient. The interlock
already prevents further release, so observing the skip does not establish a
model's payload-saving benefit.

The [maintainer runtime commands](../agents/starship-mission-chat-contract.md#public-runtime-verification)
exercise each boundary with real HTTP. The short gimbal smoke does not verify
the longer deployment-supervision path. Live providers require a separate,
explicit opt-in; the public fixture needs no API key.

## What remains unfinished

Launch-derived catch, fresh-launch fault/wind/latency robustness, qualified
capture-versus-divert operations with later physical-model effects, and the
complete operator catch workflow remain unfinished. The tower/preburn code is
an unconnected lifecycle fixture. A near-tower initialized arm-support test is
a different experiment and cannot substitute for arriving from launch.

See the [six-DOF contract](../agents/starship-sixdof-contract.md),
[deployment supervision contract](../agents/starship-flight-supervision.md),
[catch boundary](../agents/starship-launch-connected-catch.md), and
[completion gates](../agents/starship-completion-campaign.md) for the precise limits.
