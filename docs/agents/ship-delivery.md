# Stationary-ship delivery contracts

The fixture path provides a deterministic local run for one vehicle and one payload.
It does not start the Gateway, PX4, Gazebo, hardware, an external model, VLA, or
WAM. Preserve the existing authority split: human approval binds the scenario;
rules constrain the decision; the fixture executor advances state; the verifier
checks observed fixture outcomes.

The separate `run-sitl` path starts actual local PX4 and Gazebo processes. Its
contract and evidence are described under **Opt-in PX4/Gazebo runtime** below.
The fixture-only statements in the following sections apply to `plan` / `run`.

## Runtime and CLI boundaries

`src/runtime/ship_delivery.py` exposes:

- `ShipDeliveryScenario`: validated scenario input.
- `build_ship_delivery_contract(scenario)`: frozen ordered parent contract.
- `run_ship_delivery_fixture(scenario, operator_approved=False)`: execution report.

The packaged `missionos_cli` command group provides `ship-delivery plan`,
`ship-delivery run`, and `ship-delivery px4-plan`. All accept optional
`--scenario PATH` JSON and `--output PATH`. Omitted scenario fields retain
validated defaults. All emit JSON to
standard output; `--output` saves the same material with a trailing newline.

`plan` emits the serialized scenario, parent contract material, and parent
contract SHA-256. It creates no approval. `run --approve-fixture` supplies explicit
approval for this local fixture invocation. A missing approval must block before
any stage executor runs. A report whose `status` is not `completed` produces CLI
exit status 1; malformed inputs produce a CLI error.

The installed CLI entrypoint is `packages/missionos-cli/src/missionos_cli/cli.py`.
Keep local scenario execution in `ship_delivery_command.py`, separate from
Gateway dispatch routes.

## Scenario defaults

| Field | Default | Meaning |
| --- | ---: | --- |
| `offshore_distance_m` | 1000 | Ship-to-coast distance |
| `urban_distance_m` | 200 | Coast-to-delivery distance |
| `cruise_altitude_m` | 30 | Fixture cruise altitude |
| `wind_mps` | 4 | Fixture wind speed |
| `airspeed_mps` | 12 | Requested fixture airspeed |
| `battery_wh` | 220 | Initial energy budget |
| `reserve_wh` | 40 | Required energy reserve |
| `urban_blockage_s` | 0 | Urban obstruction duration |
| `max_wait_s` | 30 | Maximum permitted wait |
| `urban_perception_ready` | true | Fixture perception-readiness input |

These are the primary controls; `plan` emits the full validated scenario,
including identities, additional policy limits, and fixed fixture scope.

These are scenario parameters, not validated airframe capabilities or operational
weather limits. Perception readiness is an input to this fixture, not evidence of
VLA/WAM invocation or a tested perception system.

## Authority and evidence

Reuse the parent coordinator's approval and stage-order semantics. Approval must
bind the exact frozen contract, including child contracts and predicate packages.
A satisfied child predicate is a prerequisite for the next stage, never a new
source of authority. Preserve the core parent's conservative identity,
shared-world, and mission-completion claim boundaries.

The ship-specific verifier separately checks fixture identity continuity and
stage outcomes. Keep run, vehicle, payload, and world identities consistent;
record movement and action outcomes rather than treating a plan or a selected
decision as execution evidence. A blocked stage prevents later stage execution.
Delivery and recovery require independent outcome checks, including payload
release/location and vehicle recovery state respectively.

Positions use local ENU metres with the stationary ship at the origin, the coast
at `(offshore_distance_m, 0, 0)`, and delivery further along the east axis. The
urban obstruction is a rectangle covering 40–60% of the urban leg and 25 metres
either side of its centreline. Its scripted clock starts at urban entry. The
verifier checks continuous path segments while that rectangle is active, also
on urban exit. AP, camera and perception systems are not instantiated.

Each receipt binds a UTC `observed_at` timestamp and content digest. Independent
verification requires receipt age within 30 seconds of `evaluated_at`, ordered
receipt clocks, consecutive fixture ticks, and coherent elapsed time and energy.
`evaluated_at` can be supplied explicitly for archived replay; that is replay
verification, not current telemetry freshness. Fixture elapsed seconds are
separate from wall-clock receipt freshness. Digests detect modification, not
authenticate an external producer. Recovery needs landed/disarmed observations
at both ends of a two-second interval on the stationary deck.

The report contains:

- `status` and `fixture_mission_completed`: bounded fixture result.
- `delivery_verified` and `recovery_verified`: independent fixture outcomes.
- `identity_continuity_verified`: continuity within this fixture's observations.
- `decisions`, `stage_receipts`, and `trajectory`: auditable fixture execution.
- `coordinator`: the existing parent sequencing and authority record.
- `limitations`: the scope of the evidence.

`physical_execution_invoked`, `px4_runtime_invoked`, `vla_invoked`, and
`wam_invoked` must remain false for this execution path. Do not promote fixture
delivery, recovery, or identity checks into physical-world claims. Do not reuse
the PX4/Gazebo delivery predicate package for fixture observations.

## Runtime verification

Exercise the actual CLI entrypoint in the configured repository environment:

```sh
python -m missionos_cli ship-delivery plan --output /tmp/ship-delivery-plan.json
python -m missionos_cli ship-delivery run --approve-fixture --output /tmp/ship-delivery-run.json
python -m missionos_cli ship-delivery run
python -m missionos_cli ship-delivery px4-plan --output /tmp/ship-delivery-px4-plan.json
```

The first two commands should exit 0 for the default scenario; the unapproved run
must exit 1 without stage execution. Inspect the saved JSON to establish fixture
completion, independent delivery/recovery verification, and false live/model
invocation flags. Also exercise blocked urban perception, infeasible return
reserve, wait/detour decisions, and evidence tampering in focused tests. These
checks cover local orchestration and fixture verification; they establish no
PX4, aerodynamic, visual-policy, maritime-landing, or multi-vehicle result.

Run all CLI smoke cases, saving fresh local reports:

```sh
python -m scripts.smoke_ship_delivery --output-dir /tmp/ship-delivery-smoke
```

This covers clear, wait, detour, denied approval, insufficient reserve, missing
perception, plan inspection and PX4 plan export. The example scenario JSON files
live under `examples/fixture_missions/ship_delivery/`.

## PX4 export boundary

`src/runtime/ship_delivery_px4.py::build_stationary_ship_px4_plan` produces a
simulator-only round-trip mission-item tape. The CLI maps only
`offshore_distance_m`, `urban_distance_m`, and `cruise_altitude_m` from the
validated scenario. The compiler uses its own synthetic home coordinates,
airspeed, and timeout envelope; fixture wind, energy, decisions, and identity
bindings are not an executed PX4 contract.

A successful `px4-plan` export exits 0 while its report status remains
`blocked_pending_runtime_adapter`. This distinction means export succeeded and
dispatch is unsupported. No approval, upload, model invocation, or PX4 run is
performed. Invalid parameters for the PX4 compiler produce a CLI error.

The legacy AUTO probe recompiles an outbound mission and then conditionally
commands RTL; it does not consume this exported round-trip tape. The timed
dropoff dwell neither releases cargo nor gates departure on verified delivery.
The separate SITL adapter below implements a gated two-mission path instead of
executing this legacy tape. Preserve the export's blocked reasons in user-facing
results.

## Opt-in PX4/Gazebo runtime

`src/runtime/ship_delivery_sitl.py` exposes `build_ship_sitl_missions`,
`run_ship_delivery_sitl`, `verify_ship_sitl_delivery`, and `verify_ship_sitl_run`.
The container-local executor is `scripts/ship_delivery_sitl_worker.py`; the world
builder is `src/runtime/ship_delivery_world.py`. No Gateway restart is required:
the packaged CLI directly invokes this local adapter.

The defaults below describe Step 1. Optional `--urban-case` and `--urban-policy`
select the separate [Step 2 urban experiment](ship-urban-decisions.md).

```sh
python -m missionos_cli ship-delivery run-sitl --approve-sitl \
  --output-dir /tmp/ship-sitl-run
```

Prerequisites are a configured repository Python environment, local Docker, and
the `px4io/px4-sitl-gazebo:latest` image already installed. The report captures its
actual image ID. The command does not download an image, expose host ports, use
hardware endpoints, or invoke models. Start with at least 500 MiB of available
disk space. Raw topic streams are gzip-compressed as they are recorded.

`--approve-sitl` applies to this bounded simulator invocation. Without it, the
command fails before starting Docker or creating the output directory. The
directory must be new. `--timeout-seconds` is a finite 120–1800 s wall-clock bound
(default 900); a separate host watchdog bounds executor lifetime. Cleanup targets
only the invocation's uniquely named container. A runtime failure blocks success
and is saved with diagnostics when the output filesystem remains writable.

The runtime reuses validated scenario parameters, but declares
`execution_backend=px4_gazebo_sitl`. Its report uses `scenario_parameters`, omitting
the fixture's backend label. `urban_blockage_s` must be zero and cruise altitude
at least 25 m. Fixture decisions/perception readiness are not live perception.

World and execution contract:

- Coordinates are Gazebo ENU, with the route running **north along y**. This
  differs from the fixture's east-axis route. Synthetic home is 35.3195,
  138.7435. Deck top is z=0; the stationary ship model origin is z=-1.
- Default coast is y=1000 and delivery pad center y=1200, z=0.05. A visual sea
  has no supporting collision plane; the seabed is below it. Six static
  buildings flank the clear corridor. These are synthetic geometry, with no
  marine hydrodynamics or moving obstacles.
- The same Gazebo x500 carries a 50 g cargo body via a detachable joint. The
  executor publishes detach; it never teleports the parcel to the pad.
- Outbound AUTO ends in `MAV_CMD_NAV_LOITER_UNLIM` at 3 m. The host verifier must
  observe cargo carried aloft, then separated by >1.5 m from the vehicle, within
  6 m of the delivery center, in contact with the pad, and stable for 3 s.
- The host writes a return permit binding run, world, plan, observation count,
  and observation digest. Return upload follows only after that permit. PX4
  exits the old mission through observed AUTO LOITER before receiving the new
  mission; acceptance alone does not establish flight or delivery.
  After each upload, the executor observes a changed PX4 mission ID, the expected
  item count, and successful Navigator validation before requesting AUTO MISSION.
- Final recovery requires the same vehicle within 3 m of the deck center,
  fresh deck contact, PX4 landed/disarmed state, speed below 0.3 m/s, and at
  least 3 s of stability. Observation gaps over 1.5 s reset stability. Delivery
  remains verified through the final observation interval.
  The model origin may be up to 2 cm below deck height because its origin is not
  the feet's contact point. Position alone never establishes landing.
- Wind starts calm, then the worker applies the requested northward speed after
  climbing above 20 m and reads Gazebo's wind service back. Default speed is
  4 m/s and force scaling is 0.05, uncalibrated. This is wind invocation evidence,
  not validation of strong-wind flight or takeoff under wind.
- PX4's simulated battery is time-based (`SIM_BAT_DRAIN=1800`), not a calibrated
  energy model. The minimum observed remaining fraction must meet the scenario's
  reserve fraction; a recharge after disarm cannot erase a low in-flight value.
  The fixture's Wh and power values do not establish physical endurance.

`result.json` reports `simulation_mission_completed`, `delivery_verified`, and
`recovery_verified` separately. `px4_runtime_invoked` / `gazebo_runtime_invoked`
describe actual process startup; `physical_execution_invoked`, `vla_invoked`,
and `wam_invoked` remain false. This adapter does not promote the generic parent
coordinator's fixture receipts into simulator or physical evidence.

The output includes `config.json`, hashed world/model SDF files, both generated
mission upload scripts, `telemetry.jsonl`, `events.jsonl`, raw PX4 readings,
compressed Gazebo topic streams, simulator logs, and the return permit. Entity
IDs must remain stable; ship position, run/world bindings, event order, accepted
uploads, permit digest, and wind readback are checked independently. Hashes
detect mutation; they do not authenticate a hostile telemetry producer.
The canonical `runtime_invocation_evidence.v1` binds the actual container worker
invocation, UTC times, exit status, and hash-verified stdout/stderr artifacts.

For runtime verification, execute the command above from a fresh output
directory, inspect `result.json`, and require both outcome verifiers plus
`simulation_mission_completed=true`. A manually repaired flight is diagnostic
only; rerun the final adapter without intervention. Generated run data stays
outside the repository. Unit tests alone are insufficient for SITL readiness.

## Next milestones

1. Qualify native VLA/WAM against predeclared absolute accuracy, safety, latency,
   and cost bounds, then verify their integration in the same delivery/return
   flight. Follow the [Step 2 contract](ship-onboard-step2.md); superiority over
   an idealized rule planner is not an acceptance or compute-admission gate.
2. Add moving-ship rendezvous and recovery, then deck motion and relevant wind.
3. Add shared airspace, launch/recovery scheduling, and energy-priority handling
   while scaling from two vehicles to ten.

Each milestone needs its own observed runtime evidence. A mission export, model
call, or successful single stage is insufficient for the complete capability.
