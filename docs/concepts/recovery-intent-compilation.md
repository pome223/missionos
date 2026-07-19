# Recovery Intent, Compilation, and Verification

MissionOS keeps four different questions separate during Recovery:

1. What should the vehicle do?
2. Can that intent become a concrete bounded maneuver without changing its
   meaning?
3. Is the maneuver supported by current evidence and conservative limits?
4. What effect was actually observed after execution?

The Runtime Recovery Agent answers the first question. It may choose to
monitor, reroute, avoid locally, hold, or return/land, and may state constraints
such as direction, clearance, destination, duration, speed, or altitude.

The compiler answers the second question. It may turn those constraints into
concrete setpoints, but it may not silently change the action, direction,
destination, or safety envelope. If it cannot preserve the intent, the result
is infeasible and goes back for another proposal.

The pre-dispatch guard answers the third question using fresh telemetry. It
checks the compiled maneuver against conservative reachability, time, wind,
battery, geofence, freshness, and approval-envelope bounds. A human still
approves the concrete bounded artifact; compilation and verification do not
create approval or dispatch authority.

The outcome verifier answers the fourth question from runtime evidence. An ACK
only confirms that a command was accepted. Recovery success requires the
bounded effect, such as reaching the approved target, to be observed. AUTO may
resume only when the target and resume-safety checks both pass.

```text
LLM intent
  -> meaning-preserving compilation
  -> conservative pre-dispatch verification
  -> human approval
  -> fresh dispatch-time verification
  -> executor
  -> outcome verification
  -> repair loop when needed
```

This structure keeps the LLM responsible for mission-level judgment while
preventing an unverified proposal, accepted ACK, or generated artifact from
being mistaken for authority or physical success.
