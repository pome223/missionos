# Interrupted city decisions and bounded AP recovery

Recovery was implemented, but **no recovered hold, fresh decision or resumed segment was observed in these three CPU PX4/Gazebo trials**. The first failed on a duplicate simulator clock snapshot (fixed); the 1 m retry stopped at 1.0706 m drift; the explicitly mapped 3 m protocol stopped at 3.0071 m drift during the 9.1535 m/s city gust. Every run passed its first three normal holds and acknowledged shutdown while moving. Cargo delivery and return remain unverified in these runs. Models and GPU were not invoked.

Revocation binds run/config/cycle/attempt and is checked before/after host work. Recovery retains AP loiter, accepts repeated clock snapshots without crediting time, and requires 5 uninterrupted simulator seconds within 0.5 m, 0.3 m/s and 0.03 rad, unchanged estimator state and reserve. It is bounded to 90 wall seconds and 3 attempts. Bad reserve/state, model errors, image-gate rejection and movement after dispatch remain outside automatic retry. The optional 3 m volume has >20 m mapped clearance, including the possible 1 m initial offset; this is a different protocol from the original 1 m trial, not an unchanged-bound comparison.

The CPU live-mailbox checks exercise late startup/inference response revocation, restart, old-attempt authorization rejection and shutdown while moving. Full local suite: 3517 passed / 2 skipped before the final anchor guard; final targeted checks with map dependencies: 17 passed. Frozen flight-source hashes and final-source checks remain distinct: the final additional city-anchor precheck was CPU-tested after the last flight was launched. No final-source full-flight rerun is claimed.

[Japanese report](REPORT-ja.md), [videos/replay](index.html), [all recovery records](recovery-summary.json), [next occupied-pad design](../yokohama-pad-queue/PLAN-ja.md).

## E2E / Runtime Verification

With simulator dependencies available, each output directory must be new:

```sh
python scripts/yokohama_sitl.py --phase flight --approve-sitl --output-dir RUN \
  --decision-backend fixture --wam-profile motion-v4 --sea-round-trip \
  --deliver-payload --wind-profile harbor-nominal --wind-after-takeoff \
  --gust-seed 20260928 --recover-city-hold --recovery-radius-m 3 --timeout-seconds 2400
python scripts/verify_yokohama_recovery.py --run-dir RUN
python scripts/verify_yokohama_decisions.py RUN --output RUN/decision-verification.json
python scripts/export_yokohama_wind_report.py --run RUN --output REPORT
python -m pytest -q tests/test_yokohama_recovery.py
python scripts/screen_yokohama_pad_queue.py --output NEW-SCREEN.json
```

The first two flights used the 1 m default without `--recovery-radius-m`. All recovery/full-mission verifications remain failed. Wind command/position/physical witness checks pass for all three recorded intervals; the final gust end and subsequent recovery were not observed. Owned containers were removed; incremental GPU cost was $0 (prior estimate $16.7063 / $17). The synthetic global wind force and time-based SITL battery are not calibrated airframe wind/power measurements.
