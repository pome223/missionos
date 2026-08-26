# Was the saved robot state actually recoverable?

A policy can fail because it chose the wrong actions. It can also fail because
the saved state was already impossible to recover, the action interface could
not express the needed motion, or the action budget was too short.

We separated those explanations for one saved GR00T / LIBERO state. GR00T N1.7
did not improve the missing target in five runs with target-specific language
or five runs with the benchmark task language. A privileged scripted control,
using the same simulator action interface, restored the same state in 61
actions and kept it successful for 20 more settling actions.

So this one state was recoverable within the test budget. The result does not
give a general GR00T failure rate, and the privileged control is not an
autonomous recovery system. It only makes the negative policy result easier to
interpret honestly.
