# missionos-gateway

HTTP and WebSocket Gateway package.

The package exposes the `missionos-gateway` entrypoint used by the CLI. It has
two backend modes:

- `fixture` (default): deterministic public-safe loopback routes.
- `production`: copied MissionOS backend from `src.gateway.server`, enabled by
  `MISSIONOS_GATEWAY_BACKEND=production`.

The CLI sets `MISSIONOS_GATEWAY_BACKEND=production` when started with
`--enable-live-sitl`.

```bash
missionos gateway start
missionos gateway restart --enable-live-sitl
```

Fixture mode does not invoke Docker, PX4/Gazebo, MAVLink upload, delivery
completion, or physical execution. Production mode still keeps live SITL behind
explicit opt-in environment flags.
