# MissionOS Boundaries

MissionOS should be useful even when the answer is "not proven yet."

That requires keeping similar-looking facts separate:

- A proposal is not approval.
- Approval is not dispatch.
- Dispatch is not an ACK.
- An ACK is not runtime progress.
- Runtime progress is not landing.
- Landing is not payload delivery.
- Simulator execution is not physical execution.
- A map is evidence display, not a verifier verdict by itself.

## Why This Matters

Robotics systems often fail in the space between "we asked for something" and
"the world actually changed." MissionOS treats that space as the product surface.

The CLI and Gateway should therefore show what each layer knows, while avoiding
language that turns weak evidence into a strong success claim.

## Public Demo Rule

If a demo did not prove delivery completion or physical execution, say so
directly. A good public demo can still be valuable when it shows a bounded
proposal, operator approval, dispatch ACK, partial runtime evidence, or a clean
fail-closed recovery path.
