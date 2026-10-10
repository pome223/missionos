# Reconstructed Yokohama candidate recovery

This implementation starts from `cc4078e4` and surviving October 4 endpoint
snapshots. It is a new reconstruction, not a restoration of the lost October 5
implementation or its source hash. Its own source closure and flight evidence
must qualify it.

The CPU-only scenario takes off at the inland launch point, executes one bounded
fixture model segment, then submits a valid level fixture candidate that
overshoots the remaining goal. Unchanged progress Rules reject it before
upload/activation. The worker closes
the model session permanently and checks the host's revocation receipt before
using a separate fixed recovery authority. It uploads the approved exit at the
same launch XY/cruise altitude, observes a stable return, commands landing, and
waits for distinct stable Gazebo/PX4 observations plus fresh launch-pad contact.

The mission remains `failed` and the worker returns `failed_recovered` only after
observing return, landing and disarm. The runner keeps a nonzero mission exit.
The independent verifier decides recovery success from saved telemetry; a worker
summary, PX4 mode switch or removed container never proves landing. Cleanup is
reported separately. No native VLA/WAM, API, GPU or physical execution is claimed.

## Fixed authority and conditions

- Only `--endpoint-feedback --candidate-recovery --fixture-reject-second-candidate`
  with `--decision-backend fixture --wam-profile motion-v4 --phase flight` is
  admitted for this reconstruction. Other endpoint runtime remains blocked.
- Fresh explicit approval binds the full Python source closure, shell entrypoint,
  scene assets, immutable local image, exact argument catalog and recovery limits.
  `flight-attempt-claimed.json` is created atomically before starting resources;
  the same approval cannot replay after a crash. A separate persistent attempt
  ledger records the single authorized attempt. Output must be new.
- Overall flight timeout is 900 seconds. Recovery reserves exactly 220 seconds
  including model shutdown, upload, return, landing and terminal observations.
  Check `overall_deadline >= begin + 220`; do not subtract first, extend a deadline
  or grant a new window to a retry.
- Keep the original 4 m goal, two model cycles, 0.5 m candidate corridor, 1 m
  tracking tube, 2 m obstacle clearance and cruise-height bounds. Before flight,
  check the entire tracking corridor and landing column against frozen geometry.
- Return tolerance is 0.25 m horizontal / 0.15 m vertical, terminal landing radius
  less than 1.5 m, resting height at most 0.5 m, velocity at most 0.3 m/s. Require
  at least three distinct pose/simulation timestamps spanning two simulation
  seconds; repeated timestamps never earn stable-time credit.
- Require healthy local Docker, the existing fixed image, 6 CPU/6 GiB minimum,
  no conflicting/unknown-owner experiment, and at least 2 GiB plus 512 MiB output
  and 64 MiB finalization headroom. No image pull or weakened start gate.

## Independent offline verification

`python scripts/verify_yokohama_candidate_recovery.py RUN --output VERDICT.json`
reopens the approval, runtime source hashes, world/model hashes, mailbox order,
rejection and shutdown receipts, mission-upload/altitude transport evidence,
raw PX4 landed/arming/position telemetry, independently recorded Gazebo poses,
trajectory and contact sensor events. It requires the first dispatched segment,
second rejected candidate, no subsequent model authority and the approved return
dispatch after revocation. It checks observed translation, corridor/height,
estimator continuity, reserve/deadline, stable return and terminal contact/disarm.

The observation verifier implements its own terminal predicates and stability
calculation rather than importing runtime success predicates. Geometry and
altitude transport use the established frozen-map/transport validators. Evidence
is local simulator evidence, not cryptographically authenticated physical data.

## Persistence and cleanup

Use a permanent repository and sibling evidence directory outside `/tmp`.
Keep source provenance, operator approval, attempt ledger, logs and source-to-run
bindings. Save code/tests/docs and a small qualification result locally in Git;
push/PR/merge are separate permissions. Preserve an independently verified Git
bundle and compressed local evidence, checking all file hashes and a restored
sample before any disposal. Keep the unpushed checkout and original evidence.
Stop/remove only containers whose ID, owner label and mounts match this run;
shared images, volumes and other work are excluded. Additional flights require
new explicit approval.
