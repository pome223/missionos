# Starship-inspired simulation through MissionOS

**A chat request becomes a displayed simulation plan. The operator approves
that plan once, the simulator executes in a separate process, and the verifier
checks the receipt and saved outputs.** This bounded operator workflow runs in
the public fixture.

The separate saved full-flight example includes launch, stage separation,
orbital conditions, 26 finite satellite releases and a Ship return attempt.
Position, attitude, angular rate, fuel and actuators evolve in the simulator;
the recorded states drive the synchronized 2D/3D replay.

## Watch the recorded simulations

The [offline replay and media bundle](../assets/starship-mission-20261006/index.html)
contains three separately labelled records:

| Record | What it shows | Limit |
| --- | --- | --- |
| [Launch and 26-payload release](../assets/starship-mission-20261006/nominal-flight.mp4) | Launch, separation, orbital conditions, 26 independent payload bodies, and Ship return | Low-speed hull contact is not validated landing; the booster did not reach tower catch |
| [Missing-release supervision](../assets/starship-mission-20261006/deployment-supervision.mp4) | Fault routing (Jev), a fixed skip procedure, Rules, and later measured sequencer states | The conditional reasoning model (DeepSeek) was not called; no payload separated, and the mission did not complete |
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

## Launch and payload release: the Starlink V3 reference

SpaceX's [Flight 14 report](https://www.spacex.com/launches/starship-flight-14)
reports the release of 26 Starlink V3 satellites and initial contact. Satellite
checkout and orbit raising are subsequent operational stages. That provides
the mission reference for this example. The current simulator covers the launch and rigid-body release
portion, with each released body continuing on its own trajectory.

“V3” identifies two different things here: the vehicle profile is inspired by
Starship V3; the 26-payload mission references Starlink V3. The payload model is
parameterized rather than an identified V3 spacecraft. Its current assumed
mass, inertia and separation impulse are described in the
[six-DOF contract](../agents/starship-sixdof-contract.md).
The 3D satellites are illustrative shapes; their visible panels are not a
computed unfolding sequence. Antenna/solar-array deployment, collision-free
extraction from the dispenser, link acquisition, orbit raising and customer
service remain outside this model.

To request the full numerical launch-and-release scenario, use
`Starship sixdof_launch` in chat or **Launch + 26 payload releases** in the
operator console, then review and approve that plan before running it. The
short setup check below uses `Starship sixdof_gimbal_step`. Neither choice
invokes a real spacecraft or proves completion of Starlink commissioning.

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
The dedicated Starship launcher defaults its model backend and planner to off;
`--fixture-planner` enables this deterministic path explicitly. Generic
MissionOS chat's DeepSeek default is a separate entrypoint. Live Starship
providers require their own explicit opt-in rather than inheriting that default.

The same Gateway serves the operator console at
`http://127.0.0.1:18794/missionos/starship/operator`. It keeps plan, approval,
execution, verification, and replay controls separate. The returned `verified`
status means saved-record integrity passed; it does not mean successful return,
capture, mission completion, or authenticated human identity.

Stop the Gateway after the example. Use fresh state/output directories for
another attempt so earlier failed or incomplete evidence is preserved.

## What the operating loop verifies

The regular flight route binds a plan to its software and profile, consumes
approval once, starts a simulator with provider keys omitted from its process
environment, and checks its signed receipt and served artifact hashes.
Unapproved, stale, source-changed, duplicate, and cross-session requests are
rejected. The distinct opt-in live `dispenser_jev_shadow` worker is a provider
observer/broker and receives its own `TYPESAFE_API_KEY`; it is not a flight
simulator. Environment filtering and local signing operate under the same OS
user and do not promise OS credential isolation or authenticated human identity.

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
