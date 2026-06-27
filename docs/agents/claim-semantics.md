# Claim Semantics

Use explicit claim language. Do not collapse separate facts.

## Canonical Split

```text
LLM judges.
Human approves.
Rules constrain.
Executor acts.
Verifier checks.
Repair loops.
```

## Common Fields

- `proposal_created`: an AI or planner produced a candidate response.
- `approval_recorded`: a human approval or rejection was recorded.
- `dispatch_authority_created`: a gate created authority to send a bounded
  action.
- `dispatch_request_sent`: an executor attempted to send a command.
- `command_ack_observed`: a runtime or transport acknowledgement was observed.
- `runtime_progress_observed`: the system observed runtime movement or state
  change beyond ACK.
- `landing_observed`: landing evidence was observed.
- `delivery_completion_claimed`: the verifier claims delivery completion.
- `physical_execution_invoked`: a physical-world execution path was invoked.

## Artifact Truth Versus Runtime Truth

Stored artifacts can prove that MissionOS wrote a record. They do not by
themselves prove that runtime execution happened.

Use runtime evidence, such as HTTP loopback calls, subprocess execution,
simulator telemetry, MAVLink ACK/readback, or hardware readback, before claiming
runtime execution.
