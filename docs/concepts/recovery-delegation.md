# Recovery Delegation

Recovery is the moment a mission is most tempting to rush: something went
wrong, and the fastest fix is to let the agent just handle it. MissionOS
keeps recovery inside the same control-tower structure as the rest of the
mission instead of treating it as an escape hatch — see
`docs/concepts/boundaries.md` for the boundary language this builds on.

Recovery delegation is opt-in and experimental. It widens what the LLM is
trusted to propose during a fault, not what it is trusted to execute.

## The Two-Phase Split

When something goes wrong mid-mission, MissionOS separates the immediate
safety response from the judgment call about what to do next:

```text
Fault detected
  -> Reflex phase: deterministic, immediate, bounded
  -> Deliberation phase: LLM proposes, human approves, as always
```

The reflex phase exists because an LLM call takes time, and a robot that has
just lost track of its situation should not wait on a model response before
it stops moving or starts leaving itself a safety margin. The reflex is
deterministic code, not a model — it buys the deliberation phase time to
happen safely.

- **TurtleBot3 (ground):** the reflex is stop-first. On a qualifying fault,
  MissionOS dispatches a real navigation-stack cancel through the existing
  execution boundary, then waits for the LLM's recovery proposal.
- **PX4 (air):** the reflex is a budgeted loiter. MissionOS computes how much
  deliberation time remains before battery reserve is exhausted. If that
  budget runs out before a human has approved a recovery action, MissionOS
  dispatches a bounded return-to-launch. Within the recovery-delegation
  paths this document covers, this is the one where a fallback action fires
  without a human approval recorded for that instance — see "Reflex
  Authority" below for why that is a deliberately separate category, not a
  precedent for widening approval elsewhere.

After the reflex, the LLM proposes the next action exactly as it does for
any other recovery decision: a human approves or rejects it, and only an
approved action crosses the execution boundary.

## Shadow Measurement

Every recovery event also records what a simple deterministic rule would
have proposed, next to what the LLM actually proposed, and whether they
agreed. This comparison is measurement only. It does not gate, veto, or
replace the LLM's proposal in the moment — it accumulates evidence over
time about which actions the LLM reliably converges on with the
deterministic floor.

The LLM is never removed from the recovery loop by this measurement. Its
only effect is downstream, through the promotion path below.

## Perception Claims

When a camera is available, MissionOS can attach a **perception claim** — a
VLM's read of one frame — as evidence for a recovery proposal. A perception
claim is bound to the sha256 hash of the exact frame it was computed from,
so it cannot be about a different frame than the one on record. Corroboration
labels (matching evidence from a non-camera sensor, such as a lidar
costmap) are computed by MissionOS itself from independently observed data
— never trusted from the vision pipeline's own self-report.

The asymmetry is the safety property: an uncorroborated camera-only claim
may support conservative, fail-safe actions (hold, safe stop, return home,
ask a human) but never a progressive action (like routing around an
obstacle). A corroborated claim may support either. The guard enforces this
by blocking a progressive proposal outright whenever an uncorroborated
claim is present, whether or not the proposal cites it — a plausible but
wrong VLM read cannot, by itself, authorize the robot to go further into a
situation it misread.

Gazebo in headless CI produces synthetic camera frames, not a real scene, so
tests exercise this path with fixture data. The frame-hash binding and
corroboration guard behave identically regardless of whether the frame came
from a real camera or a fixture. Capture failure is fail-open for the
mission (the mission continues rather than blocking), but is fail-closed
for authority: a failed capture produces no perception claim and grants no
additional execution authority.

## Promotion: Earning Narrower Human Approval

The shadow-agreement history feeds one more thing: a proposal that a
specific recovery action, after a long enough streak of LLM/deterministic
agreement, no longer needs a human approval on every occurrence. This
proposal is evidence only — nothing in MissionOS applies it automatically.
An operator reviews the evidence and, if they agree, applies it explicitly
through a CLI that requires a non-empty approval reference. Applying a
promotion narrows the set of actions that still require per-instance human
approval; it never widens the LLM's execution authority, and it never
removes the LLM from proposing the next action.

## Reflex Authority Is Not Approval

The PX4 budgeted-loiter reflex's return-to-launch is dispatched without a
human approval recorded for that specific instance. This is intentionally
kept in a separate authority category from every other dispatch path in
MissionOS, which hard-requires an operator approval record before anything
crosses the execution boundary. Reflex authority exists only to keep a
budget-exhausted aircraft from running out of options while waiting on a
human; it is bounded to one safe fallback action, fires at most once per
exhaustion event, and does not generalize — nothing else in MissionOS may
skip approval by analogy to this path.

## Where To Go Next

- `docs/concepts/boundaries.md` for the general claim-boundary language this
  extends.
- `docs/agents/claim-semantics.md` for the exact field names recovery
  delegation adds and how they compose with the existing claim boundary.
