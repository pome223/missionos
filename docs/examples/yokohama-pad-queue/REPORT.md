# Aircraft-requested occupied-pad supervision

A zero-wind, CPU PX4/Gazebo round trip completed offshore departure, waiting for a preceding aircraft to leave the pad, cargo release, independently verified simulated receipt, ship return, landing and disarming. The aircraft reported occupancy to a host-side MissionOS fixture, received a wait proposal, re-observed clearance, and checked fresh execution constraints before AP approach.

[Videos and measured replay](index.html) · [Japanese report](REPORT-ja.md)

- Occupancy report to entry permission: **31.500 simulator seconds**.
- Clear pad and approach: at least **5 uninterrupted simulator seconds** before the continuation proposal.
- Minimum sampled aircraft separation: **12.817 m**.
- Normal 30-second AP holds: **13/13 passed**.
- Recorded interval: **1285.720 simulator seconds**, **3,849 telemetry samples**.
- Pre-touchdown time-based simulated battery: **64.6%**.
- Incremental GPU spend **$0**; prior estimated cumulative spend **$16.7063 / $17**.

The delivery aircraft is flown by PX4. The lead aircraft and its unloading are scripted Gazebo poses, not a second autopilot or a physically validated cargo mechanism. Occupancy uses measured simulator poses; MissionOS uses a deterministic CPU fixture. No image perception, native VLA/WAM, training, or model-added-value comparison was performed. The lead schedule is not supplied to the judge. The fixed pad camera is an explanatory view, not evidence of visibility in a VLA onboard input.

The first full trial stopped after the observed wait because a lead-pose service acknowledgement was missing. Actor-only diagnosis confirmed that pose changes can still take effect despite missing replies. Intermediate clock-start and acknowledgement-count failures remain preserved. The corrected worker bases clearance on observed poses and bounds retries by the existing 90 simulator / 180 wall second wait deadline. The final full flight recorded 7 missing-acknowledgement episodes. No aircraft hold, separation, reserve or receipt threshold was loosened. [All attempts](attempts.json)

A separate six-case CPU fixture passes aircraft wind-assistance reports through the shared MissionAssuranceAgent: reachable preapproved sea refuge, no reachable refuge, verified land hold, calm plus fresh route/stability, stale route, and inadequate reserve. These are proposals only. Route/wind feasibility is authored input, not computed or measured here. No wind diversion or post-gust AP resumption is demonstrated; the earlier 9.15 m/s recovery failure remains unresolved.

Validation: **3545 passed, 2 skipped, 3 warnings in 125.12s (0:02:05)**; actual isolated CPU PX4/Gazebo flight, host/aircraft atomic-file mailbox, independent cargo receiver, three read-only verifiers, source-bound video export. The flight-time source is hash-bound. After launch, the host cleanup was additionally guarded so a supervisor-close exception cannot skip other cleanup; this final exception-path edit is not claimed as another whole-flight rerun. All owned simulation containers were removed. Recorded positions/contact checks do not prove continuous collision-free motion or qualify hardware. Battery is time-based, without wind/inference power or Wh metering. Sea model inference and warmup are absent.

Commands and publication evidence: [Japanese methods](REPORT-ja.md), [protocol](protocol.json), [whole flight](verification.json), [pad supervision](pad-verification.json), [cargo receipt](payload-verification.json), [wind proposal fixture](wind-response-fixture.json), [source hashes](evidence-manifest.json), [scene attribution](../yokohama-urban-scene/ATTRIBUTION.md).

An opt-in short SITL check injected a supervisor-close error after worker timeout; remaining receiver/container cleanup passed. [Cleanup check](cleanup-verification.json)

After landing, simulated remaining charge rose from about 64.6% to 76.0% in the final sample. This is not observed charging. The reported flight-end indicator uses the last airborne sample (SIM 1288.100 s, 64.616%); raw values remain preserved.
