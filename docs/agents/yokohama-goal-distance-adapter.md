# Vehicle goal-distance adapter

This opt-in feature shortens a decoded VLA translation when its endpoint
would pass the plane through the approved nearby goal. It keeps the original
translation direction and commanded yaw. It cannot extend a translation, steer
toward the goal, repair an invalid raw action, relax Rules, or grant dispatch.

The exact `decisions.goal_distance_adapter` policy is
`yokohama.vehicle-goal-distance-adapter.v1`. It requires the existing stationary,
inland, four-metre endpoint-feedback contract. For observation P, raw endpoint Q
and goal G, the scale is `min(1, dot(G-P, Q-P) / ||Q-P||²)`. A nonpositive projection
is rejected. The raw leg must already be level and between 0.5 and 3 metres. The
selected endpoint must still satisfy the original 0.5 metre minimum, progress,
corridor, altitude, mapped clearance, freshness and WAM forecast checks. Within
goal tolerance, a subminimum candidate is rejected; this adapter does not invent
an arrival or landing result.

`vehicle-distance-adjustment.json` preserves the original decoded candidate and
bins, executed candidate, scale, distances and reason. It binds the exact config,
policy, original observation and raw VLA response by SHA-256. Raw bins describe
the original action even when its executed translation is shorter. A rejected
adaptation records the original candidate and rejection reason with no executed
candidate. WAM evaluates the selected immutable endpoint from its fresh camera
observation. The same adjustment is recomputed before WAM, authorization and
activation, and propagated in the prepared and activated permits. Worker and
offline verifiers reject changed or missing adjustment records.

Default configuration preserves raw-output behavior. An adapter-enabled verdict
cannot claim raw VLA output control. Native inference, adapter-assisted control,
original mission outcome, rejection recovery and physical delivery remain
separate claims. Existing expiry and irrevocable session revocation still apply.

## Qualification and execution limit

`--endpoint-feedback --goal-distance-adapter --plan-only` compiles a source-bound
plan without Docker, model calls or flight. Live use additionally requires the
separate exact `--endpoint-adapter-trial` catalog and a source-bound single-use
approval; see [native endpoint admission](yokohama-native-endpoint-admission.md).
The October 9 CPU rejection/return qualification at commit `a8beaa08` predates
this adapter; it is retained as independent evidence and must not be relabelled
as an adapter or native-model flight.

The dedicated endpoint controller, shared CPU-double/native rejection return,
and offline verifier are implemented. Normal tests qualify their contracts;
they do not qualify live execution. GPU creation requires a fresh successful CPU
endpoint flight from the full final source closure, plus exact-plan USD 8 approval (received; freeze final plan before execution).
The earlier rejection recovery is insufficient for this new gate.

The proposed infrastructure is one Oregon `g2-standard-16` VM with one L4,
200 GiB balanced boot disk and ephemeral IPv4. The provider absolute DELETE
deadline is at most one hour from reservation, with boot-disk auto-delete and
numeric identity checked controller cleanup. The controller checks tunnel liveness
and remaining cleanup time; this does not constitute a guaranteed dollar cap. No fallback VM or capacity retry is permitted. The local total
flight limit is 900 seconds. Endpoint work stops by the earlier of its 600-second
session deadline and worker elapsed 665 seconds, retaining the fixed 220-second
return window and five-second trigger margin before the ten-second worker cutoff.

Cloud upload is limited to reviewed service/bootstrap/lifecycle source and the
existing motion-adapter weight. Models download immutable public weight
revisions on the VM. Model requests contain current/past onboard camera RGBD,
poses, intrinsics and the approved goal prompt; no future simulator truth,
collision-map oracle, task database, credentials, repository archive or unrelated
private evidence is supplied to a model. Preserve small results in Git and raw
evidence locally before deleting only numeric resource identities owned by the
trial. Keep the unpushed checkout and a verified Git bundle.

On the owned VM only, IPv4 non-loopback packet quotas are installed before
upload/bootstrap: 1 GiB outbound, 64 GiB inbound. Global IPv6 addresses cause
admission failure. Quota exhaustion blocks traffic and requires cleanup through
the independent GCP API. No firewall or network policy of a persistent resource
is changed. Provider DELETE and disk auto-delete remain required.
