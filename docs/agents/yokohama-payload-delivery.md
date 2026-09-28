# Yokohama simulated cargo delivery

`--deliver-payload` extends the opt-in `--sea-round-trip --phase flight` scenario.
It does not enable hardware or a cloud service. A decision backend remains an
explicit choice; fixture decisions are not native VLA/WAM evidence.

The frozen scenario carries one 50 g, 12 × 12 × 8 cm box from the stationary
deck. The box is a separate dynamic Gazebo model attached to the x500 by the
existing [Gazebo DetachableJoint system](https://gazebosim.org/api/sim/8/classgz_1_1sim_1_1systems_1_1DetachableJoint.html).
There is no teleport, pose setter, respawn at the destination or scripted fall.
After D1/D2 model use has ended, AP reaches the existing delivery waypoint,
descends to 3 m above the authored 4 × 4 m delivery pad, and holds. The cargo
release is an executor command constrained by the operator-approved simulator
scenario and observed release Rules, not an LLM command or an approval inferred
from arrival.

## Separate facts

1. `payload_airborne_observed`: the same cargo entity took off with the vehicle.
2. `payload_release_requested`: the approved low hold is observed, fresh, armed,
   airborne, within 0.6 m of its target and moving at no more than 0.3 m/s. Cargo
   must still be within 0.8 m of the vehicle. Models must already be stopped.
3. `payload_detach_command`: a Gazebo transport command was published. Up to
   three idempotent sends are permitted, five seconds apart, only while the
   observed cargo remains attached. This event does not prove separation.
4. A separate host `PayloadReceiver` observes cargo/vehicle poses, a subsequent
   `detached` joint-state event and actual delivery-pad contact. It emits a
   receipt only after the same cargo travelled over 900 m during the outbound
   sea phase and then rested on the delivery pad for at least three simulator
   seconds. Cargo is never moved by this receiver.
5. `payload_received`: the worker accepts a fresh receipt bound to this run,
   complete configuration and the exact release request.
6. `payload_return_authorized`: AP can upload the climb and resume the return
   route. No receipt means a bounded failure and cleanup of the owned simulator;
   a timeout cannot authorize a return.
7. Existing independent flight checks still require return position, recent
   deck contact, landing and disarm. Cargo receipt alone is not mission return.

The receiving predicate requires a pad-centre distance at most 1.5 m, parcel
centre height within 0.08 m of pad top plus half cargo height, vehicle/cargo
separation of at least 2 m, and a fresh contact pair naming both the cargo and
delivery pad. Cargo pose and contact freshness are bounded at 0.5 seconds. The
stable window permits at most 0.05 m displacement, 0.1 m/s observed cargo speed
and a one-second sample gap. A receipt is accepted within five worker-clock
seconds of its final observation. Simulator timestamps and receiver wall times
are separate. Transport joint-state messages have no sensor timestamp; their
`observed_sim_s` records the observer's current simulator clock on reception.

## Evidence and reproduction

```sh
python scripts/yokohama_sitl.py --phase flight --approve-sitl --sea-round-trip \
  --deliver-payload --decision-backend fixture --wam-profile motion-v4 \
  --output-dir /tmp/yokohama-cargo-fixture --timeout-seconds 2400
python scripts/verify_yokohama_sitl.py /tmp/yokohama-cargo-fixture \
  --output /tmp/yokohama-cargo-fixture/verification.json
python scripts/verify_yokohama_decisions.py /tmp/yokohama-cargo-fixture \
  --output /tmp/yokohama-cargo-fixture/decision-verification.json
python scripts/verify_yokohama_payload.py /tmp/yokohama-cargo-fixture \
  --output /tmp/yokohama-cargo-fixture/payload-verification.json
```

Flight telemetry includes independently observed cargo poses. Separate files
record the release request, joint-state transitions, pad contacts, receiver
assessment and receipt. The receipt contains the exact stable observations and
their digest. The read-only cargo verifier reopens the original logs up to that
receipt's observed cutoff, recomputes the receipt and checks request → command →
receipt → return ordering. The flight verifier calls it in addition to its
geometry, image-depth, thirteen-hold and return checks. The decision verifier
qualifies only model decisions and keeps its separate payload claim false.

`payload-video-frames.json` indexes a fixed Gazebo camera viewing the delivery
pad during descent, release, receiving verification and climb. These are actual
simulator camera frames, distinct from the onboard footage, measured-position
replay and WAM forecasts.

This is a **simulated receiving station**, not a human recipient confirmation.
The stock contact/rigid-body model does not establish package integrity, a
qualified release mechanism, strong-wind delivery, moving-deck recovery,
multi-drone scheduling, onboard energy savings or physical-world delivery.
Native city-model success from a prior run cannot be combined with a new CPU
cargo flight and called same-sortie native cargo delivery.
