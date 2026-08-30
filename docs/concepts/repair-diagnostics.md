# Repair Diagnostics

A Repair attempt should not be reduced to one success or failure bit. MissionOS
records five separate questions:

1. Did the executor produce a meaningful action?
2. Was that action aligned with a corrective reference?
3. Did the missing predicate recover in the observed world?
4. Were already-satisfied predicates preserved?
5. Did the recovered state remain valid for the required hold period?

The first two questions diagnose model behavior. The last three require observed
simulator, runtime, or physical effects; a model prediction is not enough.

The same representation also works for older evidence. If an earlier run
recorded predicate recovery and preservation but did not retain action or
alignment traces, those missing stages stay `not_observed`. MissionOS does not
rewrite missing evidence as success or failure merely to make a report look
complete.

Passing all five axes supports only a bounded statement about the evaluated
task, fixture, executor, and observation scope. It does not create approval or
dispatch authority, prove mission completion, establish general Repair
capability, or imply physical execution.
