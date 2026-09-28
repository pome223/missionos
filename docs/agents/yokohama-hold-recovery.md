# City decision interruption and AP hold recovery

`scripts/yokohama_sitl.py --recover-city-hold` is an opt-in CPU fixture
extension. It is not enabled for native models or physical aircraft.
The existing two city decision cycles and AP-only sea transit remain in force.

Before AP mission activation, a transient speed, position or camera/estimated
heading change raises `HoldInterrupted`. The worker atomically revokes the
current `(run, config, cycle, attempt)` and stops consuming its responses.
The host checks revocation before and after each operation, clears pending
authority, and rejects obsolete attempts. A model operation already running
may finish; revocation is not a claim of GPU cancellation. A started lifecycle
is reconciled with `resume`; it is not started a second time.

The existing AP loiter target remains unchanged. Recovery requires:

- AP loiter, armed and airborne; valid position; reserve at least 20%; unchanged
  estimator reset counters and finite telemetry throughout.
- Every sampled position within the recovery radius of the original decision anchor, with
  nondecreasing simulator times and no sample gap greater than 2 s. Repeated
  clock snapshots earn no additional stable time; all sampled motion is checked.
- Five uninterrupted simulator seconds within 0.5 m, speed at most 0.3 m/s,
  and both estimated and physical heading drift at most 0.03 rad.
- At most 90 wall seconds per recovery and three decision attempts per cycle.

The default recovery radius is 1 m. An explicit `--recovery-radius-m` accepts
1..3 m only after preflight mapped-volume clearance is checked at both city
holds. The volume includes the possible 1 m anchor offset and the inverse
world-to-source transform scale; its full height range must retain more than
2 m clearance from relevant frozen building footprints. Store the map hash and
recompute this certificate in the verifier. A larger recovery volume does not
change the 0.5 m / 0.3 m/s / 5 s requirement for a new decision, the original
30-second arrival holds, or the 1 m tracking tube after dispatch. Radius trials
are different protocols and must be reported separately, including failures.

New attempts use distinct mailbox sequences, capture directories and upload
scripts. Both VLA and WAM histories are collected after the recovered hold;
old images, proposals and upload permissions are not reused. Mission upload
and activation authorization are inside the retry boundary; AP activation
and subsequent movement are outside it. The existing tracking tube and
arrival verifier still apply after dispatch.

Low reserve, invalid telemetry, lost AP/arming authority, estimator resets,
exceeded recovery bounds, model/HTTP failures, invalid permits and rejected
image consistency remain fatal. Recovery never changes the WAM threshold.
Stop requests continue sampling but do not require the aircraft to be still.
Host cleanup remains an independent finalizer.

`verify_yokohama_recovery.py` reopens sampled recovery windows, distinct fresh
captures and observed segment arrival. Its result concerns recovery of a city
decision only. Delivery, cargo receipt, return and whole-flight wind-force
verification are separate results. Unit/mailbox fixtures do not prove PX4
motion or native model value.
