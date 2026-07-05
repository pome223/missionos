# Unitree Go2 Real Hardware

This page documents the real Unitree Go2 branch. It is separate from
`docs/agents/unitree-mujoco-environment.md`: MuJoCo provides a low-level
simulator bridge, while a real Go2 provides the onboard `sport` service that
`SportClient.Move` targets.

## Boundary

The first real-Go2 slice is read-only service readiness. It:

- initializes Unitree SDK2 only after an explicit opt-in
- requires an operator-provided non-loopback robot network interface
- requires real-hardware DDS domain id `0`
- calls `SportClient.GetServerApiVersion`
- calls `RobotStateClient.ServiceList`
- does not call `SportClient.Move`
- does not publish `rt/lowcmd`
- does not send a MissionOS dispatch request
- does not claim physical execution

## Readiness Smoke

Run only from a machine intentionally connected to the Go2 robot network:

```bash
RUN_MISSIONOS_UNITREE_GO2_REAL_HARDWARE_READINESS=1 \
UNITREE_GO2_NETWORK_INTERFACE=en0 \
UNITREE_GO2_DOMAIN_ID=0 \
UNITREE_GO2_PYTHON_EXECUTABLE=/path/to/unitree-sdk2-venv/bin/python \
PYTHONPATH=.:/path/to/unitree_sdk2_python \
/path/to/unitree-sdk2-venv/bin/python \
scripts/smoke_unitree_go2_real_hardware_readiness.py
```

When the gate is not enabled, the smoke exits successfully with `ran=false` and
does not import SDK2 or touch the robot network.

`readiness_status=ready` means the onboard sport service and robot-state service
answered read-only metadata calls. It is not movement evidence and not operator
approval for movement.

## Next Slice

Only after readiness is `ready`, a later slice may add a bench/cage bounded move
smoke. That future smoke must require a separate movement opt-in, operator
approval, a clear area attestation, and an emergency stop plan. It must keep
raw `rt/lowcmd`, raw motor, raw velocity, and special-motion commands blocked.
