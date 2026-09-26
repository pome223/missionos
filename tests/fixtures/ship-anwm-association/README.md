These reviewed synthetic PNGs and NumPy arrays are extracted from the original
four native-model simulator trials on 2026-09-26. They contain only the final
pre-inference observation and the two generated candidate views. The geometry
contains depth, camera poses and intrinsics, without future frames, obstacle
truth, host paths or service credentials. Source history hashes and derived
file hashes are in `manifest.json`.

These are development regressions, not held-out native flight validation. The
original 1 km flight remains failed. The revised decoder must qualify in a new
frozen flight cohort before a mission-completion claim.
