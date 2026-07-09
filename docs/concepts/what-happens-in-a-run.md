# What Happens in a MissionOS Run

MissionOS turns a single "mission" into a sequence of distinct, auditable facts.

The LLM can only **propose**. A human must **approve**. Rules and the
Gateway **constrain** what can be sent. An executor **acts**. A verifier
records what was **actually observed**. Nothing is collapsed into "it worked."

## The Core Sequence

```text
operator request
  -> LLM or planner produces a bounded proposal
  -> human reviews the proposal
     -> rejected:
          rejection recorded
          dispatch authority not created
     -> approved:
          approval recorded
          dispatch authority created
          bounded action sent to the execution boundary
          command ACK observed, timed out, or rejected
          runtime progress observed from telemetry, pose, or status
          verifier checks the evidence
          narrow claim recorded, or the run remains blocked
```

Every step remains a separate fact in the task timeline. Each arrow is a real
boundary: crossing it creates a record, and not crossing it is also a record.

## What Each Stage Actually Means

- **Proposal**: An AI or planner produced a candidate action or route. This is
  **not** an order. It has no authority yet.

- **Approval / Rejection**: A human explicitly allowed (or denied) the specific bounded action.
  The Gateway only creates dispatch authority after a recorded approval.

- **Dispatch request sent**: The system sent the approved action across the execution boundary (to a simulator adapter, Nav2, PX4 bench, etc.).

- **ACK observed**: The executor or vehicle controller acknowledged receipt of the command.
  An ACK is **not** proof that the robot moved or that the goal will be reached.

- **Runtime progress observed**: Actual state change was measured (pose delta, Nav2 status, PX4 telemetry, etc.).

- **Verification & Claim**: The verifier looks at the collected evidence and decides what can be claimed.
  Claims stay narrow — for example, "a simulator action completed" rather than
  "the mission was delivered." In public snapshots, physical execution and
  delivery completion are almost always still marked as not claimed.
  (The exact field names live in [claim semantics](../agents/claim-semantics.md).)

## Why the Separation Matters

If any step is missing or fails its checks, later steps are blocked or clearly marked as unproven.

- A nice map image does not mean the robot followed the plan.
- A successful ACK does not mean the mission completed.
- Simulator motion does not mean physical execution happened.

MissionOS exists so that when something goes wrong (or succeeds), you can point to the exact recorded facts instead of telling a story.

## See Also

- [Boundaries](boundaries.md) — the claim rules in plain language
- [Agent Roles](agent-roles.md) — who proposes vs who approves
- [Examples](../examples/README.md) — real runs with their evidence and limitations
- [Agent contracts](../agents/contracts.md) — the mechanical rules the code must obey
- [Claim semantics](../agents/claim-semantics.md) — the exact field names and what they mean

All public demos and quickstarts are required to state what was proven and what remains unproven.
