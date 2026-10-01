# Yokohama CPU SITL boundary

The opt-in `scripts/yokohama_sitl.py` loads the frozen [urban scene](yokohama-urban-scene.md)
into a disposable PX4/Gazebo container. Its default `contacts` and `flight` modes
invoke no model service. There is no hardware, remote vehicle or cloud provisioning
endpoint. The explicit `--decision-backend` option adds the separately qualified
[city model-view boundary](yokohama-native-flight.md); it does not change the
historical fixed-AP evidence or establish native inference by itself.
Run-specific observations are written to a new explicit output directory.

## Source and physics

The importer validates the published hashes before converting source vertices.
D1's local ground height plus 0.12 m defines a synthetic launch pad top. A local
inverse-projection/geodesic Jacobian maps source projected coordinates to true
ENU; source vertical coordinates are rebased, never sent directly as home-relative
altitude. The world spherical origin has synthetic altitude zero. The source
model is not a surveyed flight map or an accurate GPS/height-error model.

Source visual meshes retain their geometry. OBJ normals are generated per face;
zero-area faces are omitted and counted in the manifest. DART otherwise ignores
meshes lacking normals and may fail while constructing collisions. Physics uses
the conservative building/bridge prisms and the source terrain TIN. Contact
sensors use Gazebo's observed default scoped topics, not an assumed custom alias.
The 4 m square launch/destination pads are authored scenario elements.

The `contacts` world starts paused. Observation subscriptions precede unpausing.
A sphere deliberately intersects one selected building prism; a second drops onto
terrain; a third remains in the D2 corridor with gravity disabled. Require both
positive contact identities and the negative control. The rolling ground sphere
is a contact/vertical-support check, not proof of full horizontal rest.

## AP measurements

The default flight is a fixed, operator-authorized AP route: D1 → D2 → D3 → destination →
D3 → D2 → D1. The operator approved this simulation through `--approve-sitl`.
No model chose these waypoints. Each leg is at most 20 m. MAVLink upload receipt,
Navigator validation, observed AUTO_MISSION, arrival, AUTO_LOITER, and measured
hold are separate facts. An upload ACK or mode transition does not establish motion.

At each point, settle for at least 3 simulator seconds, then observe an uninterrupted
30-second hold. Frozen bounds: horizontal target error ≤1 m, vertical error ≤0.6 m,
PX4 speed ≤0.5 m/s, AUTO_LOITER throughout, armed and airborne, fresh independent
Gazebo poses, and sample gaps ≤2 simulator seconds. All seven holds, return to the
launch pad, landing, disarming, and contact with that pad are required. Any observed
vehicle/city contact fails the flight. Payload release and delivery are absent.

PX4 and Gazebo clock/position observations remain in the private raw evidence.
The read-only verifier recomputes holds, source/model/world/file bindings, CPU and
network isolation, return state, and contact events. The explicit `hold_started`
event and sample count distinguish the hold start from a preceding settling
query that shares a stats tick; within-hold timestamps must strictly increase. A separate swept polyline
check screens a 1 m assumed vehicle radius against height-overlapping source
prisms. This sampled check does not certify an unobserved continuous trajectory.

## Camera and native-model readiness

CPU-rendered fixed and onboard RGB-D cameras use 640×360 pixels, 60° horizontal
FOV, and 2 Hz. Onboard downward RGB is also recorded. The fixed D2 camera is
compared with ray intersections in the original source mesh on a fixed 24-pixel
grid, using observed intrinsics and pixel centers; tolerance is 0.05 m. The
collision-probe world adds visible spheres, so this unmodified source-only depth
check runs in the flight world. Images and actual recorded trajectories can be
replayed; they are not native WAM future frames.

This integration does **not** connect `ship_urban_loop` or qualify native VLA/WAM.
The [ANWM reader](ship-anwm-static.md) has a restricted colored-obstacle contract.
Before spending on models, replace/extend that reader for ordinary buildings,
freeze valid/ambiguous/blocked cases, and bind fresh images, native proposals,
predictions, Rules, approval scope, dispatch ACKs, observed arrival, and lifecycle
shutdown to the urban loop. Keep model calls and warmup inside the city. The
default city-only test excludes the 1 km sea leg and moving ship. The optional
stationary offshore extension below adds the sea leg. Idealized Rules need
not be beaten; absolute arrival, safety, latency, and cost bounds apply.

## Reproduction and publication

### Stationary offshore extension

`--sea-round-trip` adds an authored stationary 12 m deck, visual-only water and
an AP-only outbound/return route to the same world and vehicle. The synthetic
coastal gateway is 400 m from D1 along a source-frame 15-degree ray; the ship is
another 1,000 m away. This gateway is beyond the source crop, **not a surveyed
shoreline**. The source meshes and city route remain source-bound. The complete
approach is checked against height-overlapping building prisms before launch.

The route is ship takeoff → coastal gateway → the seven city stops above →
coastal gateway → ship hold → landing/disarm. Sea cruise is 8 m/s; the city and
inbound connector to D1 use 3 m/s. All eleven stops require the same 30-second
measured hold. Gazebo remains referenced to D1; the vehicle spawns on the ship
and PX4 observes its home there. Landing checks use the ship position and require
recent deck contact, rather than counting the initial takeoff contact.

For model segments, the measured PX4 global altitude minus observed home altitude
is mapped to the simultaneous Gazebo world height. Moving launch offshore changes
the relative-home datum; using city world z directly left a retained CPU attempt
about 0.19 m above its second model goal. Upload now uses observed relative altitude
plus the model's requested world-height difference. Bind this mapping to the permit,
reject a >0.05 m offset change during upload, and preserve the original 0.15 m
observed vertical arrival bound. This is simulator observation-based frame mapping,
not new altitude-estimation or hardware qualification.

Model warm-up and inference still begin only after the D1 hold. Both the worker
and host reject startup/inference unless the request phase is the approved D1/D2
cycle and its observed vehicle position is within 1 m of that city stop. Cleanup
stop remains available everywhere. Shutdown after D2 and late-authority rejection
precede the remaining AP route, including both outbound sea stages. The verifier
reopens requests, all eleven holds, both 1 km measured sea movements, model-off
events, and final recovery. The separate decision verifier does not itself certify
the sea trajectory; require both verifiers for the combined claim.

```sh
python scripts/yokohama_sitl.py --phase flight --approve-sitl --sea-round-trip \
  --decision-backend fixture --wam-profile motion-v4 \
  --output-dir /tmp/yokohama-sea-city --timeout-seconds 2200
python scripts/verify_yokohama_sitl.py /tmp/yokohama-sea-city \
  --output /tmp/yokohama-sea-city/verification.json
python scripts/verify_yokohama_decisions.py /tmp/yokohama-sea-city \
  --output /tmp/yokohama-sea-city/decision-verification.json
```

Fixture decisions establish control integration only. A native run requires an
explicit native service configuration and its separately authorized compute
budget. Sea transit alone does not add cargo release. The separate opt-in
[`--deliver-payload` extension](yokohama-payload-delivery.md) adds a dynamic
50 g parcel, two extra AP holds, contact/rest-based simulated receipt and
receipt-gated return. It requires all three flight, decision and cargo
verifiers. Neither extension qualifies moving-deck recovery, wind,
multi-vehicle scheduling or a validated energy model.

Use the dependency versions from the scene bundle in a dedicated environment.
Use the image ID recorded in each result; the tested arm64 image is resolved
locally, and the CLI never pulls it automatically. Network mode is `none`, device
requests are empty, and `LIBGL_ALWAYS_SOFTWARE=1` is set. The dedicated container
is removed after success or failure. This command consumes local CPU, not paid GPU.

```sh
python scripts/yokohama_sitl.py --phase contacts --approve-sitl \
  --output-dir /tmp/yokohama-contacts --timeout-seconds 140
python scripts/yokohama_sitl.py --phase flight --approve-sitl \
  --output-dir /tmp/yokohama-flight --timeout-seconds 1200
python scripts/verify_yokohama_sitl.py /tmp/yokohama-flight \
  --output /tmp/yokohama-flight-verification.json
```

Keep unsuccessful development attempts. Freeze the source and qualification
limits before the final cohort. Publish reviewed metrics, camera captures,
reduced trajectory, reproduction code, and evidence hashes; never copy raw
container inspection or workstation paths into the public report.

## Opt-in wind and battery display

The [wind and battery contract](yokohama-wind-battery.md) adds uniform wind
stress and an offline battery-overlay video exporter. These extensions preserve
the default zero-wind behavior. Wind CPU fixtures and time-based simulated
remaining charge are separate from native-model or energy qualification.
