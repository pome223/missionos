# PX4 Core live-E2E publication summary

Date: 2026-07-24

This is a sanitized maintainer summary of reviewed, opt-in PX4/Gazebo SITL
evidence. The raw Task record, local simulator output, model transcript, and
credential-bearing environment are not public artifacts.

The reviewed source run kept the following facts separate:

1. MissionOS Core returned `verified_feasible`.
2. DeepSeek selected a recovery candidate but created neither approval nor
   dispatch authority.
3. A human explicitly approved the exact pending proposal.
4. The Gateway revalidated current Hazard State and policy before creating
   bounded dispatch authority.
5. Runner ACK was stored separately from observed motion.
6. The verifier observed the recovery target, AUTO route resume, route
   completion, RTL, landing, and disarm.
7. `chat`, `job-status`, `operate`, `watch`, and `map` referred to the same
   source Task.

The source Task identifier is represented only by SHA-256 in the machine
readiness index. The record is simulator evidence, not physical flight or
payload-delivery evidence. Both `delivery_completion_claimed` and
`physical_execution_invoked` remained false.

The anonymous PX4 corpus in
`tests/golden/action_feasibility/px4_v1/` preserves the reviewed feasibility
and authority boundaries for offline replay. Replay invokes no model,
simulator, approval, dispatch, or execution authority.
