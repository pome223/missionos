# missionos-sitl

Simulator adapter package.

The repository now includes copied PX4/Gazebo SITL runtime modules under
`src/runtime/`, simulator helpers under `simulators/`, and smoke scripts under
`scripts/`.

Live SITL remains opt-in:

```bash
missionos gateway restart --enable-live-sitl
```

Without that flag, the CLI uses fixture Gateway responses. With that flag, the
Gateway starts the copied production backend and enables the SITL execution
environment variables. Docker/PX4/Gazebo readiness, MAVLink upload, runtime
progress, dropoff verification, delivery completion, and physical execution are
still separate facts and must be verified independently.
