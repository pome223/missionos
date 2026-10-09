# Vehicle goal-distance adapter

This opt-in planning feature shortens a decoded VLA translation when its endpoint
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
plan without Docker, model calls or flight. The CLI rejects live use of this flag.
The October 9 CPU rejection/return qualification at commit `a8beaa08` predates
this adapter; it is retained as independent evidence and must not be relabelled
as an adapter or native-model flight.

Before a future real VLA/WAM trial, implement and review a separate native
endpoint admission/controller path, native rejection-to-return integration and
an applicable live-evidence verifier. Bind these final sources, services, image,
config, operator approval and a one-attempt budget ledger. The existing native
trial runner contains a different sea/payload mission and cannot be reused
unchanged for this experiment. Offline tests here do not qualify live execution.

The proposed infrastructure is one Oregon `g2-standard-16` VM with one L4, a
200 GiB balanced boot disk, one ephemeral IPv4 address, provider-side absolute
DELETE deadline within one hour, local watchdog and identity-checked cleanup.
No service account/scopes, firewall expansion, static IP, snapshots, cloud
storage or fallback VM are needed. Use the existing SSH key and SSH tunnels to
loopback model services. Limit the trial to two VLA and two WAM requests, one
600-second endpoint session, and keep the fixed rejection recovery reserve.
The pending paid-trial approval and source-bound native admission are execution
gates. Do not start resources merely because this planning feature passed tests.

Cloud upload is limited to reviewed service/bootstrap/lifecycle source and the
existing motion-adapter weight. Models download immutable public weight
revisions on the VM. Model requests contain current/past onboard camera RGBD,
poses, intrinsics and the approved goal prompt; no future simulator truth,
collision-map oracle, task database, credentials, repository archive or unrelated
private evidence is supplied to a model. Preserve small results in Git and raw
evidence locally before deleting only numeric resource identities owned by the
trial. Keep the unpushed checkout and a verified Git bundle.
