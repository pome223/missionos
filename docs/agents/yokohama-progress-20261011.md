# Yokohama evidence through the offline comparison attempts

AP return previously entered its fixed hold without a separately observed
settling phase. The eight-metre CPU scope now requires distinct stable home
observations before starting the unchanged thirty-second hold. Faults after
model authority revocation use the existing bounded return/land/disarm path.
Historical failures remain failures; a recovered failed mission is not delivery
success. This continuation leaves the original full-delivery catalog unchanged.

| Scope | Observed result | Claim boundary |
| --- | --- | --- |
| Earlier full delivery | Four CPU scenarios and one native-model-assisted PX4/Gazebo full-delivery trial were independently verified | The native run used real VLA/WAM for two D1 legs through the approved goal-distance adapter; the remaining route was authored. Simulated cargo receipt, ship return, landing and disarm are not physical delivery. |
| Visual input and reobservation | Actual frozen Gazebo RGB changed CPU color-double output between open and blocked scenes; three blocked observations and two reobservations closed without authority | Interface/Rule wiring only. This backend is not learned AeroVLA perception. |
| Final calm and weak-gust continuation | Both reached the 8m goal, returned, settled, held for thirty seconds, landed and disarmed in CPU PX4/Gazebo simulation | One 0.5m/s eastward pulse for two simulation seconds, fresh reobservation and mapped clearance checks. No GPU effect or aircraft safety claim. |
| Revoked-session and already-home AP faults | Original missions failed and bounded recovery passed | The already-home AP case uses fresh stable home evidence instead of a fictitious second translation; unrelated cases retain their translation checks. |
| Native saved-image open/blocked comparison | Not completed | Cache preflight abort, a terminal capacity failure, a secured VM's pre-inference logging failure, and two later HTTP503 requests are retained. No learned avoidance qualification resulted. |

The successful calm/gust and two fault cases were flown on `d2e734f`.
`08fb9ce` changed two offline verifiers afterward; control, generator, decoder
and configuration were unchanged. Original failing verdicts and final scoped
verdicts are both preserved. The source-correspondence record states this split
explicitly rather than presenting the final closure as byte-identical to flight.
Route, heading, altitude, freshness, waiting and deadline thresholds were not
relaxed to obtain the passing cases.

The later cloud repair writes file evidence before optional display/journal
output, removes the unsupported delete `--async`, checks startup failures,
provides independent REST observation/deletion, and binds immutable VM/boot-disk
ownership. Pending inserts are not resource absence. The opt-in guest uses
source hashes, a one-attempt reservation, conservative network quotas and
monotonic child deadlines. The original 600-second deletion failure remains a
failure. A later explicit approval allowed a 900-second profile with earlier
normal/watchdog/cloud deletion guards; completion is still not guaranteed by
the provider.

The 14:14 UTC and 22:49 UTC single create commands both returned HTTP503 without
an accepted insert receipt. SDK and independent REST checks observed no matching
VM/disk/operation; post-deadline checks also found no reserved address. All owned
monitors stopped. No insert DONE state was fabricated and no unbound delete was
issued. No warmup, real saved-image comparison, WAM or flight was run in either
request. The USD 1 comparison group retains its prior USD 0.4568919543 estimate,
USD 0.5431080457 remainder and cumulative USD 1.7512037928 estimate under USD 8;
reservations stay USD 7. Invoices are unconfirmed and another trial requires new
approval. Hourly execution remains disabled.

## Evidence and reproduction

Small public summaries identify source, scope and limitations. Raw simulator
traces, approvals, account-specific controllers, error/SDK logs and cloud
identities remain in restricted local archives. The latest archive is about
1.46MB; its companion summary records SHA-256 and complete streamed content
verification. Existing large archives and the previously verified Git bundles
were retained rather than copied again.

- [Earlier full delivery](yokohama-delivery-feedback-results-20261009.json)
- [Initial visual/gust failure and image-pair wiring](yokohama-visual-gust-qualification.json)
- [Heading continuation](yokohama-heading-continuation-qualification.json)
- [Settled return, calm/gust and retained fault verdicts](yokohama-return-settling-qualification.json)
- [Visual/return contract and opt-in replay requirements](yokohama-visual-gust.md)
- [Offline comparison boundary](yokohama-offline-vla-pair.md)
- [Logging/deletion repair](yokohama-lifecycle-repair-20261011.md)
- [900-second profile](yokohama-offline-vla-900s-profile.md)
- [First HTTP503 request](yokohama-offline-vla-900s-result-20261011.md)
- [Latest HTTP503 retry and preservation](yokohama-offline-vla-retry-result-20261011.md)

For CPU-only contract checks, run the related Yokohama tests listed in the PR.
`python scripts/smoke_yokohama_vehicle_service_gateway.py` exercises a real
loopback Gateway/worker/CLI boundary with fixture planning, Docker inventory,
flight and verifier results. `python scripts/smoke_ship_urban_loop.py --output-dir
/path/to/fresh-fixture-output` exercises CLI/process/HTTP fixtures and cleanup.
These commands invoke no Docker daemon, GPU, actual SITL flight or model API.
Actual simulator/native trials remain opt-in and require separate source-bound
approval; recorded trial commands are not permission to repeat them.
