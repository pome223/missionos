# Nav2 Core live-E2E publication summary

Date: 2026-07-24

This is a sanitized maintainer summary of reviewed, opt-in
TurtleBot3/Nav2/Gazebo house-simulator evidence. The raw Task record, local
simulator output, model transcript, and credential-bearing environment are not
public artifacts.

The reviewed source run kept the following facts separate:

1. MissionOS Core returned `verified_feasible` for a source-backed 3D bypass.
2. DeepSeek selected the candidate but created neither approval nor dispatch
   authority.
3. A human explicitly approved the exact pending checkpoint.
4. The Gateway refreshed costmap evidence and revalidated the candidate before
   creating bounded dispatch authority.
5. Nav2 ACK was stored separately from observed odometry and clearance.
6. The verifier observed target tolerance, route resume, and terminal
   simulator route completion.
7. `chat`, `job-status`, `operate`, `watch`, and `map` referred to the same
   source Task.

The source Task identifier is represented only by SHA-256 in the machine
readiness index. The record is simulator evidence, not physical robot
execution or payload-delivery evidence. Both `delivery_completion_claimed`
and `physical_execution_invoked` remained false.

The anonymous Nav2 corpus in
`tests/golden/action_feasibility/nav2_v1/` preserves the reviewed feasibility
and authority boundaries for offline replay. Replay invokes no model, ROS,
Nav2, simulator, approval, dispatch, or execution authority.
