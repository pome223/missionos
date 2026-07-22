# Replay Evidence

A MissionOS replay bundle is a publication-safe record of what happened during
one simulator mission. It lets another person check the order of events without
receiving the private task database.

The bundle keeps these facts separate:

```text
AI judgment
→ compiled bounded action
→ human approval
→ dispatch authority
→ command acknowledgement
→ observed executor effect
→ target arrival
→ verified route resume
```

It also includes bounded local-coordinate telemetry and terminal observations.
It does not include raw task IDs, operator identity, local paths, prompts,
credentials, or global coordinates.

A replay verifier checks hashes and references between the published artifacts.
It does not re-run the simulator, prove physical execution, or turn arrival into
a delivery-completion claim. An old run may be marked
`verified_with_limitations` when its task record kept an approval reference but
not the complete historical dispatch receipt.

See the [replay bundle example](../examples/recovery-replay-bundle.md) for a
deterministic two-Recovery fixture that exercises export and verification
without publishing a private runtime record.
