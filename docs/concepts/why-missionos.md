# Why MissionOS

MissionOS is a **robot-agnostic AI control layer** for robots and drones operating
in the physical world. It does not depend on whether a robot carries an LLM, nor on
its level of autonomy or on-board intelligence.

## The problem

Soon, tens of millions — eventually hundreds of millions — of robots will operate in
the real world. People cannot pilot each of them one by one. What is missing is a
layer that **absorbs the per-mission operations** and concentrates human attention on
the decisions that actually matter.

MissionOS is that layer. It does **not** replace the human with a single "final
approval." Situations change, and a changed situation demands a new checkpoint and
re-approval. The goal is to let a human exercise **meaningful authority at meaningful
decision points** — not to sign off once and walk away.

```text
Observe            →  Perceive / Judge  →  Extract           →  Human authority
(reliable sensing)    (LLM)                (higher-level AI)    (approval & decision)
```

## What MissionOS is — and is not

**It is not low-level control.** Real-time control belongs to the robot's own stack —
PX4 in the air, Nav2 on the ground. MissionOS continuously observes state and
**proposes intervention when a meaningful change requires a decision.**

**It is robot-agnostic, and this is demonstrated rather than asserted.** The same
authority / claim / evidence discipline applies to aerial (PX4) and ground robots, and
the backend-neutral adapter runtime is proven across **Nav2 and Unitree**
(`ros2_nav2_hardware_adapter.py`, `unitree_hardware_adapter.py`). PX4 SITL remains a
dedicated runtime. This is not a single-platform coincidence.

## What it does today

- **SITL (loopback PX4 + Gazebo):** horizontal route closed-loop and the recovery
  pipeline are matured.
- **Real hardware (bench):** limited to **arm / disarm** on a props-removed airframe,
  with ACK and state readback after approval.
- Both an **LLM-perception path** and an **LLM-free bounded-dispatch path** exist.

## What it does not claim

This honesty is part of the design, not a limitation to hide.

- **Field operation (outdoor flight, delivery) is not claimed.** Deliberately.
- **Takeoff, mission start, and delivery are excluded from the bench** — enforced in
  the type system (`takeoff_allowed: Literal[False] = False`).
- **Simulation and loopback evidence is never upgraded to physical proof.**
- A SITL `completed` result is **not** a field release gate.

## The discipline that must never collapse

- **Authority split:** the LLM only **proposes**; a human **approves**; rules and the
  Gateway **constrain**; an executor **acts**; a verifier records what was **observed**.
- **Never collapse the boundaries:**
  `proposal ≠ approval ≠ dispatch ≠ ACK ≠ progress ≠ delivery ≠ physical`.
- **Recovery proposes only** — it never self-approves or self-dispatches.

Contract tests can *fix* the discipline in place. But **choosing which discipline is
worth protecting remains a human responsibility** — and that choice must not be
delegated away.
