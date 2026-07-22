# Backend-neutral adapters

MissionOS can send a bounded action through different robot backends without
changing its approval and evidence rules for each robot.

The shared flow is:

```text
prepare a bounded action
-> human approves that exact action
-> resolve a registered adapter
-> dispatch once
-> observe ACK, state, and progress separately
-> verify only the observed adapter action
```

The adapter translates an approved action into a backend command. It cannot
approve its own proposal or declare the whole mission complete. An ACK means
only that the backend accepted a request; it is not proof of motion or success.
The approval expires quickly and belongs to one exact prepared action. Safety
telemetry must be supplied explicitly; silence is never treated as healthy.

The same conformance tests currently cover ROS2/Nav2 and Unitree MuJoCo adapter
contracts. A loopback Gateway smoke also exercises the Unitree path through a
real subprocess boundary. That smoke proves a simulated adapter action only. It
does not prove a running MuJoCo world, physical robot motion, delivery, or
mission completion.
