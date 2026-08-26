# GR00T Language-Conditioning Check

MissionOS can record that a Repair instruction reached the GR00T policy input
and that the model produced actions. That still does not show whether changing
the instruction changed those actions.

The language-conditioning check holds one saved simulator observation and the
model sampling seed fixed. It runs the same instruction twice as a control,
then changes only the instruction:

```text
same observation + instruction A
same observation + instruction A
same observation + instruction B
```

The two A results must match before an A/B difference is accepted. A verified
A/B difference is narrow evidence that the model output was locally sensitive
to the instruction. It does not establish that the model understood the
instruction, repaired the task, produced a controller ACK, or caused a
simulator or physical effect.

The check is inference-only. It creates no approval or dispatch and applies no
policy action to the simulator.
