# Urban-only repeated decisions: controller contract

Status: CPU-only control integration. The new loop is **not connected to the
native AeroVLA/ANWM or PX4/Gazebo flight adapters**. `run-sitl` retains its prior
single-decision behavior. No new native flight, delivery, recovery or energy
saving is established by this controller.

The historical native flights used an `urban_entry` waypoint 30 m before the
coastline and warmed the cloud VLA service before takeoff. Their verified
delivery/return results remain valid, but they do not meet the new requirement
to run models only inside the city. Do not relabel those flights as urban-only.

## Authority and sequence

`UrbanLoopPlan` binds the operator approval reference, execution scope, run,
convex NED corridor, inland entry and absolute limits. Physical execution is
unsupported. The caller owns the AP approach through a separately approved
clear route; this controller refuses entry outside the corridor or wrong phase.

`UrbanLoopRuntime` performs one non-reusable session:

1. Check valid, fresh urban position and reserve; observe AP hold at the approved
   inland entry for the settling interval. Only then start the owned model
   workers. Loading/warmup belong inside this guarded startup operation.
2. Reobserve. Request a VLA candidate proposal. Reobserve again and request WAM
   assessment bound to that exact proposal. Keep observing AP hold while either
   request is pending. No candidate movement occurs before both responses.
3. Validate the selected candidate against the approved corridor/leg bound and
   independently supplied Rules. Bind both responses and a fresh observation to
   a single-use segment permit. The AP executor must validate/consume it, record
   ACK, and move. ACK alone is not arrival.
4. Observe segment tracking and stable arrival. Repeat from fresh images after
   arrival, at least twice. Reused images, old cycles and mixed runs reject.
5. Observe AP hold before exit, revoke the session and pending requests, stop
   owned model processes, and require observed shutdown before AP return handoff.
   Late responses cannot restore authority. No automatic re-entry is supported.

An inference failure, timeout, loss of position/hold, exhausted reserve, rejected
candidate or shutdown failure blocks further segment dispatch and sea return.
The mission supervisor must implement a separately preapproved, bounded abort
or diversion; indefinite hovering is not a qualified physical failure response.

Defaults: 2 updates, 30 m maximum segment, 2 s observation age, 0.5 m hold drift,
0.3 m/s held speed, 2 s settling, 0.5 s maximum observation gap, 0.25 m target
error, 120 s startup, 75 s per model response, 60 s per segment, 600 s total,
20% reserve. These are controller limits, not demonstrated aircraft capability.
Fixture tests explicitly shorten time bounds; they never qualify native latency.

## Adapter contracts

- AP: `observe`, `hold`, `dispatch(permit)`, `return_handoff`. Observations use
  local NED metres and monotonic host seconds. Preserve raw sensor/flight records
  externally; an image hash in this receipt alone does not prove camera capture.
  Sequence and observation times must increase without a gap above the bound.
- Image capture: each request needs a new hash and host capture timestamp newer
  than the last arrival. A native adapter must additionally validate full RGBD
  history and sensor-clock joins using the existing native contracts.
- Models: `start`, `infer(request)`, `stop`, `stopped`. Workers receive no flight
  authority. Their responses must bind request hash, session and model role.
  The WAM assessment must bind the VLA candidate set. Candidate adaptation and
  image prediction decoding belong to a separately reviewed native adapter.
- Rules: `authorize(start, target, observation)` returns a binding to all three,
  together with an explicit allowed result. Geometry and obstacle clearance
  cannot be replaced by a model's confidence. The included double explicitly
  represents an empty corridor; it is not an obstacle-perception implementation.
- Process ownership: `ManagedUrbanModels` starts only caller-supplied argv,
  never shell strings, cloud VMs or model downloads. Loopback workers use fresh
  process nonces. Cancellation locks out future startup and terminates/reaps
  only owned process groups. Exceptions never count as successful shutdown.

`ship_urban_loop_verifier.py` reopens request/response hashes, fresh observations,
selected targets, permits, ACKs, stable arrivals and shutdown ordering. It can
verify control sequencing, not native inference, simulator physics, delivery,
recovery or energy savings. These remain explicit false fields.

## Runtime smoke

```sh
PYTHONPATH=packages/missionos-core/src:packages/missionos-cli/src:. \
  python -c 'from missionos_cli.cli import missionos; missionos()' \
  ship-delivery urban-loop-smoke --approve-fixture \
  --output-dir /tmp/new-urban-loop-smoke
```

This starts two actual local Python processes and exchanges real HTTP requests.
The VLA/WAM responses and AP observations are explicitly synthetic. The two
selected fixture targets are NED `[1035,-5,-30]` and `[1055,0,-30]`, from inland
entry `[1015,0,-30]` with the coast at north 1000. Workers are killed/reaped before
the fixture AP return handoff. The original first response is deliberately
replayed after revocation and must be rejected.

`--fault` accepts `offshore_entry`, `wrong_phase`, `stale_observation`,
`reused_image`, `cross_cycle`, `unsafe_candidate`, `rules_rejection`,
`http_failure`, `timeout`, `hold_loss`, `shutdown_failure`, `arrival_failure`,
`tracking_deviation`, `offshore_drift`, and `reserve_exhausted`.
Each fault is expected to exit 1, preserve its blocked receipt and prevent return
handoff. Even the synthetic shutdown-failure case actually reaps its workers.

Next integration work: preserve the actual AeroVLA action grammar, ANWM input
and image-decoding evidence, and PX4 trajectory verification while adapting them
to per-cycle records and inland startup. The native services currently warm
before flight and use one-shot VLA exit; a loopback mock cannot validate this
adaptation. Run model-free SITL before spending on the new native cohort.

Power claims require measured whole-mission Wh, including propulsion while
hovering for inference, startup and compute. A stopped process or fewer calls
does not establish battery savings. No Rules-superiority comparison is required.
