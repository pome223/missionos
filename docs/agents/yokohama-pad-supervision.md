# Aircraft-requested delivery-pad supervision

`yokohama_sitl.py --occupied-pad --sea-round-trip --deliver-payload --phase flight
--approve-sitl` is opt-in, CPU-only and zero-wind. It rejects native/fixture VLA
backends in this initial stage. No hardware, remote model or paid compute is
invoked. The existing cargo receiver, offshore AP transit and return verifier
remain in use.

The lead aircraft and its parcel are authored kinematic Gazebo models. Only
these two names can be moved with `set_pose`; the delivery aircraft moves through
PX4 mission uploads. Lead unloading is a visual sequence, not a qualified second
flight controller, grasp/release mechanism or delivery receipt. Its departure
corridor is checked against every frozen building footprint. This is not a
claim of dynamic perception or WAM forecasting.

At the existing D3 hold, the delivery aircraft observes a busy pad and sends a
source-bound request to the host-side MissionOS fixture supervisor. The aircraft
holds away from the pad; it never waits directly over the earlier aircraft.
After the response says wait, the actor completes its unloading/departure
sequence. The actor's clock and schedule are not provided to the supervisor. Pose requests
are idempotent, with 250 ms transport waits and retries bounded by the existing
90 simulator / 180 wall second occupancy deadline. A reply is not occupancy evidence; only freshly
observed lead positions can clear the pad.
The CPU fixture sees current measured Gazebo poses, not RGB or future positions.

Before a continue proposal, require five uninterrupted simulator seconds with
both a clear 6 m horizontal pad exclusion zone and clear 3 m approach corridor.
Every observation must be fresh within one second, with sample gaps at most two
seconds, no repeated-time credit or estimator/entity discontinuity. AP must be
armed, airborne, in Loiter, within 0.6 m of the waiting point, at no more than
0.3 m/s, with at least 20% simulated battery remaining. The same latest-state
checks apply when accepting a response. Responses bind run/config/request ID,
sequence and source observation; maximum two seconds of worker/simulator age.

A proposal does not approve or dispatch a move. The executor checks the explicit
CLI simulator approval, permitted action set, current pad/approach state and
entry permission again before switching AP to Mission. Mission upload by itself
is not motion. Samples continue checking pad clearance and inter-aircraft
separation through delivery and return. The independent payload receipt still
gates return. Invalid observations, reoccupation, communication timeout or lost
hold bounds end this bounded owned-SITL trial; they are **not** a tested real-world
emergency landing or safe recovery. The pad advice deadline is two wall seconds;
the whole wait is bounded by 90 simulator / 180 wall seconds. No network access
is needed: host/aircraft messages cross a real atomic file mailbox.

`verify_yokohama_pad_queue.py` reopens requests/responses, trajectory and mission
events. Each decision observation must exist in the trajectory; the verifier
rechecks wait/clearance, response freshness and sequence, observed lead departure,
entry/receipt/return order and sampled separation. `verify_yokohama_sitl.py` and
`verify_yokohama_payload.py` remain separate required whole-flight checks.

## Wind-response proposal adapter

`yokohama_mission_response.py` and `check_yokohama_mission_response.py` exercise
the existing shared `MissionAssuranceAgent` with a deterministic fixture judge.
They are **not connected to the wind-flight executor** in this change. Feasible
routes, wind reachability, reserve estimates and available arrival areas are
explicit authored input facts; they are not measured or inferred by this adapter.
A sea request can propose a preapproved shore/ship refuge; it never assumes land
is always reachable. A land request can propose a verified hold. A calm-weather
request can propose fresh observation/reassessment only after ten seconds of
reported calm, vehicle stability, route revalidation, reserve and deadline checks.
The ten-second interval is a fixture protocol, not a real-aircraft operating limit.

Immediate stabilization and the preconfigured communication-loss contingency
remain aircraft/AP responsibilities. No asynchronous MissionOS reply can bypass
those limits. An unavailable/unreachable choice produces escalation referencing
the local fallback policy, not a command to hover indefinitely over water.
Connecting the proposals to measured wind-aware route feasibility and a real
bounded AP diversion is subsequent work; previous 9.15 m/s recovery remains
unachieved. Sea VLA/WAM warmup/inference stays disabled.

Simulator transport reference: [Gazebo 8 UserCommands](https://gazebosim.org/api/sim/8/classgz_1_1sim_1_1systems_1_1UserCommands.html). Service replies and subsequently observed entity motion are distinct evidence.
