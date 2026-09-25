# Urban WAM / PX4 route experiment

This opt-in experiment executes three urban route families with actual textured
Gazebo meshes: passage between buildings, climbing above a low building, and
lateral detours around a tall building. It is simulation, not physical flight.
The existing bounded lateral-flight / Jev consumer retains its original contract.

## Two explicitly selected modes

- `--selector geometric` executes the declared feasibility route.
- `--selector anwm --gpu-config <local-file>` captures a fresh airborne history,
  runs the pinned ANWM on an already provisioned GPU, ranks admissible route
  templates by predicted-image goal MSE, and executes the selected route.

ANWM predicts one **intermediate viewpoint** per route: five metres along that
route from the observed vehicle pose. All routes share a destination, so comparing
only their common endpoint would not distinguish route choices. The model receives
RGB-D history and a pose delta. It does not receive future flight observations.
The declared destination image is captured by a separate static camera before
flight and used only for image scoring. That camera is then removed.

The conditioning offset is 32 frames / nominal 8 seconds. This is an explicitly
**pose-conditioned route preview**, not a verified eight-second flight forecast.
Acceleration, waypoint dwell, dynamics and prediction-time alignment remain
unverified. RGB MSE is not collision risk, free-space occupancy or reachability.
This boundary makes one initial route choice; it does not implement receding-horizon
replanning or dynamic-obstacle avoidance.

## Independent constraints and observations

The retained user instruction authorizes the bounded SITL experiment. A fresh
HMAC trigger binds session, scene, route, instruction reference, and (in ANWM mode)
selection receipt. Rules separately check every route segment against mesh AABBs
expanded by a conservative 0.6 m enclosing sphere, with at least 0.25 m clearance.
The controller also checks observed swept segments, scene motion, telemetry,
heading, OFFBOARD authority and geofence throughout execution. A failure lands
and disarms; no alternate model or geometric dispatch fallback is substituted.

The model input's original observation may be at most 180 wall seconds old at
selection and dispatch. The vehicle must still be within 0.25 m and 0.1 rad of
that observation, in OFFBOARD hover with fresh telemetry. The returned checkpoint,
upstream code, VAE, candidate/input hashes, inference receipt and PNG hashes are
checked locally. Receipts are reproducibility bindings, not remote attestation.

The world uses a 4 ms physics step and real-time factor 0.2. Gazebo's stock world
pose publisher is limited to 60 wall-Hz; at factor 0.25, 62.5 simulation steps per
wall second can be throttled, losing exact image/pose matches. Inputs require
16 distinct consecutive 4 Hz simulation frames, within 4 ms cadence tolerance.
A broken sequence is discarded, never interpolated or duplicated. Source:
[Gazebo SceneBroadcaster](https://github.com/gazebosim/gz-sim/blob/gz-sim8/src/systems/scene_broadcaster/SceneBroadcaster.cc).

Contact publishers are subscribed separately. A positive ground-contact control
and zero building-contact messages are recorded. A silent building topic alone
does not prove contact-free flight. The reported clearance is independently
computed from sampled vehicle poses against conservative mesh bounds.

## Reproduction

Use a fresh local output directory for each run. No hardware interfaces or network
ports are exposed. The simulator container has `--network none`, scope label and
verified mounts; cleanup removes only that recorded container.

```sh
python scripts/px4_urban_wam_trial.py --phase fetch-assets --assets-dir "$ASSETS"
python scripts/px4_urban_wam_trial.py --phase screen --scene climb
RUN_PX4_URBAN_WAM_TRIAL=1 python scripts/px4_urban_wam_trial.py \
  --phase run --scene climb --assets-dir "$ASSETS" --output-dir "$RUN" \
  --approved-instruction-ref "$RETAINED_INSTRUCTION_REF"
python scripts/verify_urban_wam_trial.py --root "$RUN" --output "$RUN/verification.json"
```

`--scene` accepts `gap`, `climb`, or `detour`. For ANWM mode add `--selector anwm`
and `--gpu-config "$GPU_CONFIG"`. The local transport JSON schema is
`missionos_urban_gpu_transport.v1`, with explicit `project`, `zone`, `instance`,
`remote_root` (`/home/<user>/aerial-wam`) and `published_runtime_sha256`. The VM
must already contain that exact published `aerial_anwm_runtime.py`, pinned
upstream, checkpoint and VAE cache. This runner never provisions a VM. The operator
must enforce the approved total budget, set VM maximum lifetime / auto-delete,
and remove the VM and boot disk after collecting evidence.

Only `request.json` and `assets.npz` are uploaded by the runtime transport. Raw
captures, approval references, HMAC keys, Jev keys and cloud identifiers are local
only and must not be committed. Public results must be explicitly reviewed,
aggregated and stripped of session identities and local/cloud paths.

The Apartment mesh and textures come unchanged from
[OSRF gazebo_models](https://github.com/osrf/gazebo_models/tree/8163eb4b5e7e21985c6591d1c0bfb56468c0093f/apartment),
revision `8163eb4b5e7e21985c6591d1c0bfb56468c0093f`, CC BY 3.0, copyright
2012 Nathan Koenig; model author Cole Biesemeyer. Runtime SDF applies instance
translation and uniform scale. Visual and collision geometry use the same mesh.
The asset allowlist and SHA256 values are in `urban_navigation_contract.py`.

## Value test

Connection success and learned navigation benefit are separate outcomes. Always
report the unconstrained model choice, geometry-rejected routes, actual selected
route, projection choice and shortest admissible geometric route. A sole admissible
route supplies no evidence of learned route selection. These initial scenes already
have successful full-map geometric solutions; they establish an execution boundary,
not a WAM improvement. A value claim needs held-out same-start outcomes with
meaningfully different route costs/failures and a strong current-observation baseline.
