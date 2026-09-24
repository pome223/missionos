# Non-flight contact-probe shutdown investigation — 2026-09-24

This is infrastructure validation, separate from the closed twelve-case
[headroom cohort](urban-wam-headroom-20260924.md). It neither resumes that cohort
nor changes its `incomplete_stop` result or GPU gate.

## Failure and bounded diagnosis

In a fresh network-isolated PX4/Gazebo container, the unmodified contact probe
from commit `1f0bad3b1e0f2a58ddae9e3ed249cade3135d27e` reproduced exit **134** on
its eighth process invocation. The preceding seven exited zero. The failed
invocation wrote thirteen building-contact observations and confirmed sphere
removal before printing success and aborting with
`terminate called without an active exception`. With `PYTHONFAULTHANDLER=1`,
stderr additionally reported `Fatal Python error: Aborted` and no Python frame.
The diagnostic stopped at this first reproduction rather than retrying it into
success. The simulator aircraft controller was never started.

The failure is localized to process teardown after successful sensing/removal.
It is consistent with a native callback/interpreter teardown race, but an exact
C++ stack and causal proof are unavailable. In particular, the loopback-address
warning is not evidence that the network-isolated transport failed.

The installed Gazebo Transport is **13.5.0**, with Python **3.12.3**. Its official
[Python binding](https://github.com/gazebosim/gz-transport/blob/gz-transport13_13.5.0/python/src/transport/_gz_transport_pybind11.cc)
acquires the GIL to invoke callbacks; the underlying
[Node destructor](https://github.com/gazebosim/gz-transport/blob/gz-transport13_13.5.0/src/Node.cc)
already unsubscribes. The fix does not assume automatic cleanup was absent.
It makes callback release earlier, explicit and checked while Python is running.

## Change and verification boundary

The probe now releases both subscriptions in `finally`, including partially
registered and exceptional paths, checks that none remain, and releases its
Node reference before writing a success receipt. Cleanup failure remains an
error. A previous diagnostic receipt is invalidated on entry. No `os._exit`,
abort suppression, zero-exit substitution, retry or inference fallback is used.
The flight runner still requires the probe subprocess to exit zero; a JSON
receipt alone cannot admit flight.

The positive check uses the same real building sensor and passive sphere as the
preflight probe. The missing-topic negative control retains the same sphere and
physics but directs the contact subscription to a nonexistent sensor topic. It
must time out, remove the sphere and release subscriptions, exit **1**, and leave
no success receipt, including no stale receipt from the previous positive check.
These checks do not arm, take off, dispatch routes or invoke learned models.

The repaired probe completed **30/30** separate positive processes in the first
simulator and **20/20** in a fresh simulator, with contact, removal, explicit
release and exit zero in every case. The fresh simulator's missing-topic check
exited **1** after 35.886 seconds and left no receipt. Commander reported
**Disarmed** before and after the fresh check; no aircraft controller artifacts
were created in either session. Both owned containers were removed. There were
zero flights, learned-model calls or additional rented GPU instances.

Recorded results are in the [reviewed diagnostic summary](../assets/urban-contact-shutdown-20260924/summary.json).
Repeated processes within one simulator are shutdown stress checks, not
independent navigation cases or a collision-rate benchmark. Passing a finite
number of checks does not prove a native race is impossible on every runtime.

## Reproduction

Use a fresh caller-local `$RUN`, the previously verified pinned urban `$ASSETS`,
and the retained instruction reference. The image must already be installed.
This command starts only the isolated simulator and contact checks; it cleans up
its owned container on completion or failure.

```sh
RUN_PX4_URBAN_WAM_TRIAL=1 python scripts/check_urban_contact_shutdown.py \
  --output-dir "$RUN" --assets-dir "$ASSETS" \
  --image sha256:79968fe25aa19d51c49fbd4a863ea9380f4efe6d9afabd1c579ddeffeb8b8c93 \
  --approved-instruction-ref "$RETAINED_INSTRUCTION_REF" --attempts 20
python -m pytest tests/contract/test_urban_contact_probe.py -q
```

The standalone command checks twenty positive process exits and one expected
negative exit, without retry. It retains per-process stdout/stderr, receipt,
exit code and wall time, commander status before/after, source hash and cleanup.
The regression tests also require changed source hashes to reject the old frozen
cohort before simulator setup. Historical raw-cohort verification belongs to its
original source commit; the existing published artifact verifier remains valid.

The next research action would require a **newly registered CPU cohort** using
the repaired probe and the same predeclared headroom criteria. This diagnostic
provides no evidence of learned navigation benefit, no new training justification,
and no authorization from the measurement gate to rent another GPU.
