# Repair trials guided by experience

Saved repair procedures may work in one observed state and fail in a nearby one. When a simulator can reset to **the same start**, MissionOS can use prior measured successes to decide which saved procedure to try first. It keeps the current procedure first; if that fails, it tries a bounded alternative. Only a directly verified result at this start can justify proposing the alternative.

This lets experience change the use of a small trial budget without asking an LLM to write a new movement for each failure. A human approves the allowed scope, Rules constrain trials, the Executor acts, and the Verifier checks the result. A simulator result does not establish that a physical robot will behave the same way.

The public fixture demonstrates the proposal and verification boundary. It does not demonstrate robot recovery.
