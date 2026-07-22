# missionos-gateway

HTTP and WebSocket Gateway package.

The package exposes the `missionos-gateway` entrypoint used by the CLI. It has
three backend modes:

- `fixture` (default): deterministic public-safe loopback routes.
- `production`: the MissionOS-only backend from `src.gateway.server`, enabled
  by `MISSIONOS_GATEWAY_BACKEND=production`. Legacy general-agent, memory,
  cron, skills, runtime-tool, and tool-approval routes are absent from its
  FastAPI route table.
- `legacy-agent`: the explicitly selected general-agent backend, enabled by
  `MISSIONOS_GATEWAY_BACKEND=legacy-agent` for old internal callers only.

The CLI sets `MISSIONOS_GATEWAY_BACKEND=production` when started with
`--enable-live-sitl`.

```bash
missionos gateway start
missionos gateway restart --enable-live-sitl
```

Fixture mode does not invoke Docker, PX4/Gazebo, MAVLink upload, delivery
completion, or physical execution. Production mode still keeps live SITL behind
explicit opt-in environment flags.
