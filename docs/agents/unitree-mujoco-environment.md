# Unitree MuJoCo Environment

This page documents the external Unitree MuJoCo environment boundary for
MissionOS. MissionOS does not vendor Unitree MuJoCo, Unitree SDK2, or MuJoCo.
The operator supplies a local checkout and explicitly opts in before any
readiness smoke inspects it.

## Source

Use the upstream Unitree repository:

```text
https://github.com/unitreerobotics/unitree_mujoco
```

The current MissionOS readiness slice targets the Python simulator shape:

- `simulate_python/unitree_mujoco.py`
- `simulate_python/config.py`
- `simulate_python/unitree_sdk2py_bridge.py`
- `unitree_robots/go2/scene.xml`
- `example/python/stand_go2.py`

## Readiness Smoke

Run the readiness smoke only after cloning the external repository yourself:

```bash
RUN_MISSIONOS_UNITREE_MUJOCO_READINESS_SMOKE=1 \
UNITREE_MUJOCO_ROOT=/path/to/unitree_mujoco \
PYTHONPATH=. .venv/bin/python \
scripts/smoke_unitree_mujoco_environment_readiness.py
```

The smoke:

- reads files from `UNITREE_MUJOCO_ROOT`
- checks the Go2 Python simulator file layout
- parses `simulate_python/config.py` without importing it
- requires `ROBOT="go2"`
- requires `DOMAIN_ID=1`
- requires a loopback interface (`INTERFACE="lo"` on Linux, `INTERFACE="lo0"`
  on macOS)
- does not import Unitree SDK2
- does not start MuJoCo
- does not send a dispatch request
- does not claim physical execution

When the gate is not enabled, the smoke exits successfully with `ran=false`.

## SDK2 Import Smoke

Import Unitree SDK2 Python only after an explicit gate:

```bash
RUN_MISSIONOS_UNITREE_SDK2_IMPORT_SMOKE=1 \
PYTHONPATH=. .venv/bin/python \
scripts/smoke_unitree_sdk2_import_readiness.py
```

The smoke imports expected Python modules such as `unitree_sdk2py` and records
missing modules as blocked readiness. It does not start MuJoCo, send commands,
or claim physical execution.

If Unitree SDK2 must run in a different Python environment than the MissionOS
repo environment, pass the external interpreter explicitly:

```bash
RUN_MISSIONOS_UNITREE_SDK2_IMPORT_SMOKE=1 \
UNITREE_SDK2_PYTHON_EXECUTABLE=/path/to/unitree-sdk2-venv/bin/python \
PYTHONPATH=.:/path/to/unitree_sdk2_python \
.venv/bin/python \
scripts/smoke_unitree_sdk2_import_readiness.py
```

## MuJoCo Process Smoke

Start the external Python simulator only after both the local checkout readiness
and an explicit process gate are satisfied:

```bash
RUN_MISSIONOS_UNITREE_MUJOCO_PROCESS_SMOKE=1 \
UNITREE_MUJOCO_ROOT=/path/to/unitree_mujoco \
PYTHONPATH=. .venv/bin/python \
scripts/smoke_unitree_mujoco_process_launch.py
```

This smoke starts `simulate_python/unitree_mujoco.py`, waits briefly, and then
terminates the process. It does not import Unitree SDK2 in MissionOS and does
not send a dispatch request. A running process is not treated as scene-loaded
evidence or robot-motion evidence; those fields remain false in this slice.

If the external simulator needs a separate Python interpreter, pass it
explicitly:

```bash
RUN_MISSIONOS_UNITREE_MUJOCO_PROCESS_SMOKE=1 \
UNITREE_MUJOCO_ROOT=/path/to/unitree_mujoco \
UNITREE_MUJOCO_PYTHON_EXECUTABLE=/path/to/unitree-sdk2-venv/bin/python \
PYTHONPATH=.:/path/to/unitree_sdk2_python \
.venv/bin/python \
scripts/smoke_unitree_mujoco_process_launch.py
```

The default process wait is long enough to catch early simulator import and
viewer initialization failures. `launch_status="started"` only means the
process survived the smoke window and was terminated by the smoke. It is not
scene-loaded evidence, robot-motion evidence, or dispatch evidence.

## Headless Scene Observation Smoke

The process smoke covers Unitree's upstream viewer process. To prove that the
Go2 MuJoCo scene itself can be loaded and stepped without relying on the viewer,
run the headless scene observation smoke:

```bash
RUN_MISSIONOS_UNITREE_MUJOCO_SCENE_OBSERVATION_SMOKE=1 \
UNITREE_MUJOCO_ROOT=/path/to/unitree_mujoco \
UNITREE_MUJOCO_PYTHON_EXECUTABLE=/path/to/unitree-sdk2-venv/bin/python \
PYTHONPATH=.:/path/to/unitree_sdk2_python \
.venv/bin/python \
scripts/smoke_unitree_mujoco_scene_observation.py
```

This smoke loads `config.ROBOT_SCENE` with MuJoCo Python, steps the simulation,
and reports `scene_loaded_observed` and `robot_motion_observed` from the
resulting `qpos` delta. It does not start Unitree's viewer process, does not
initialize Unitree DDS, does not send a dispatch request, and does not claim
MissionOS control. The motion source is reported as
`uncommanded_physics_step`.

## SDK2 Bridge State Observation Smoke

After scene observation succeeds, the next bounded runtime check is the
headless SDK2 bridge observation smoke. It starts Unitree's SDK2 bridge in a
headless MuJoCo loop and observes `rt/lowstate`; it still sends no command.

```bash
RUN_MISSIONOS_UNITREE_MUJOCO_SDK2_BRIDGE_OBSERVATION_SMOKE=1 \
UNITREE_MUJOCO_ROOT=/path/to/unitree_mujoco \
UNITREE_MUJOCO_PYTHON_EXECUTABLE=/path/to/unitree-sdk2-venv/bin/python \
UNITREE_MUJOCO_SDK2_CHANNEL_INTERFACE=config \
PYTHONPATH=.:/path/to/unitree_sdk2_python \
.venv/bin/python \
scripts/smoke_unitree_mujoco_sdk2_bridge_observation.py
```

In the local Docker path, explicit SDK2 interfaces such as `lo` aborted in the
CycloneDDS native layer. The verified safe workaround is to run the container
with no external network and opt into SDK2 autodetection:

```bash
docker run --rm --network=none \
  -v "$MISSIONOS_ROOT:/work/missionos" \
  -v "$UNITREE_EXTERNAL_ROOT:/work/ext" \
  -w /work/missionos \
  missionos-unitree-smoke:local bash -lc '
export PYTHONPATH=/work/missionos:/work/ext/unitree_sdk2_python
export LD_LIBRARY_PATH=/work/ext/cyclonedds_install/lib
RUN_MISSIONOS_UNITREE_MUJOCO_SDK2_BRIDGE_OBSERVATION_SMOKE=1 \
UNITREE_MUJOCO_ROOT=/work/ext/unitree_mujoco \
UNITREE_MUJOCO_PYTHON_EXECUTABLE=/work/ext/unitree_venv/bin/python \
UNITREE_MUJOCO_SDK2_CHANNEL_INTERFACE=auto \
/work/ext/unitree_venv/bin/python \
scripts/smoke_unitree_mujoco_sdk2_bridge_observation.py
'
```

The `auto` mode is accepted only when the smoke can see no active non-loopback
network interface. This keeps DDS autodetection from silently selecting a real
robot network.

## SportClient Bounded-Move Probe

The next opt-in probe asks whether the official Go2 `SportClient.Move` surface
is available against the headless Unitree MuJoCo SDK2 bridge. It uses only a
tiny bounded velocity request and never publishes `rt/lowcmd` from MissionOS.

```bash
RUN_MISSIONOS_UNITREE_MUJOCO_SPORT_CLIENT_PROBE=1 \
UNITREE_MUJOCO_ROOT=/path/to/unitree_mujoco \
UNITREE_MUJOCO_PYTHON_EXECUTABLE=/path/to/unitree-sdk2-venv/bin/python \
UNITREE_MUJOCO_SDK2_CHANNEL_INTERFACE=config \
PYTHONPATH=.:/path/to/unitree_sdk2_python \
.venv/bin/python \
scripts/smoke_unitree_mujoco_sport_client_probe.py
```

For the local Docker path, use the same network-isolated `auto` guard as the
SDK2 bridge observation smoke:

```bash
docker run --rm --network=none \
  -v "$MISSIONOS_ROOT:/work/missionos" \
  -v "$UNITREE_EXTERNAL_ROOT:/work/ext" \
  -w /work/missionos \
  missionos-unitree-smoke:local bash -lc '
export PYTHONPATH=/work/missionos:/work/ext/unitree_sdk2_python
export LD_LIBRARY_PATH=/work/ext/cyclonedds_install/lib
RUN_MISSIONOS_UNITREE_MUJOCO_SPORT_CLIENT_PROBE=1 \
UNITREE_MUJOCO_ROOT=/work/ext/unitree_mujoco \
UNITREE_MUJOCO_PYTHON_EXECUTABLE=/work/ext/unitree_venv/bin/python \
UNITREE_MUJOCO_SDK2_CHANNEL_INTERFACE=auto \
/work/ext/unitree_venv/bin/python \
scripts/smoke_unitree_mujoco_sport_client_probe.py
'
```

`probe_status=blocked` is a meaningful result here. It means MissionOS reached
the official high-level SportClient boundary but the current upstream MuJoCo
bridge did not accept the bounded move. Do not convert that into raw low-level
motor control.

## Bounded Dispatch Smoke

MissionOS does not implement Unitree low-level motor control. Bounded dispatch
is routed through an operator-provided bridge command:

```bash
RUN_MISSIONOS_UNITREE_MUJOCO_BOUNDED_DISPATCH_SMOKE=1 \
RUN_MISSIONOS_UNITREE_MUJOCO_SMOKE=1 \
UNITREE_MUJOCO_BRIDGE_COMMAND="/path/to/unitree_bridge_command" \
PYTHONPATH=. .venv/bin/python \
scripts/smoke_unitree_mujoco_bounded_dispatch.py
```

MissionOS sends the bridge a JSON request with:

- `action=bounded_local_move`
- `physical_execution_invoked=false`
- `raw_motor_allowed=false`
- `raw_velocity_allowed=false`
- `special_motion_allowed=false`

The bridge must return JSON ACK, state, and progress receipts. If the bridge
claims `physical_execution_invoked=true`, MissionOS rejects it.

MissionOS includes a bounded command bridge for the official high-level Go2
`SportClient` surface:

```bash
RUN_MISSIONOS_UNITREE_MUJOCO_BOUNDED_DISPATCH_SMOKE=1 \
RUN_MISSIONOS_UNITREE_MUJOCO_SMOKE=1 \
RUN_MISSIONOS_UNITREE_MUJOCO_SPORT_CLIENT_BRIDGE=1 \
UNITREE_MUJOCO_ROOT=/path/to/unitree_mujoco \
UNITREE_MUJOCO_SDK2_CHANNEL_INTERFACE=auto \
UNITREE_MUJOCO_BRIDGE_COMMAND="/path/to/unitree-sdk2-venv/bin/python scripts/unitree_mujoco_sport_client_bridge.py" \
UNITREE_MUJOCO_BRIDGE_TIMEOUT_S=30 \
PYTHONPATH=.:/path/to/unitree_sdk2_python \
/path/to/unitree-sdk2-venv/bin/python \
scripts/smoke_unitree_mujoco_bounded_dispatch.py
```

This bridge may return `ack_status=rejected` when the upstream MuJoCo bridge
does not provide the Go2 sport service. That is still useful evidence: MissionOS
sent a bounded high-level request to the operator-owned bridge and did not fall
back to `rt/lowcmd`. The smoke summary includes `bridge_responses` so reviewers
can see diagnostics such as `sport_server_api_code`,
`sport_service_available`, and `robot_state_service_list_code` separately from
the adapter evidence artifact.

## Why Domain 1 And Loopback

Unitree's README recommends distinguishing simulation DDS domain id from the
real robot default and recommends loopback for simulation. MissionOS requires
`DOMAIN_ID=1` and a loopback interface for this readiness slice so the first
environment check cannot accidentally point at a real robot network.

Use:

- `INTERFACE="lo"` on Linux and Ubuntu containers
- `INTERFACE="lo0"` on macOS

Keep `USE_JOYSTICK=0` for unattended smokes unless an operator is explicitly
testing a gamepad path.

## Ubuntu External Smoke Setup

Use this flow when crossing the real Unitree SDK2/MuJoCo checkout boundary on
Linux. Keep all external checkouts outside the repository:

```bash
export MISSIONOS_ROOT=/path/to/missionos
export UNITREE_EXTERNAL_ROOT=/tmp/missionos_unitree_external

mkdir -p "$UNITREE_EXTERNAL_ROOT"
cd "$UNITREE_EXTERNAL_ROOT"
git clone https://github.com/unitreerobotics/unitree_mujoco.git
git clone https://github.com/unitreerobotics/unitree_sdk2_python.git
git clone --depth=1 --branch 0.10.2 \
  https://github.com/eclipse-cyclonedds/cyclonedds.git
```

Install the Linux build/runtime dependencies:

```bash
sudo apt-get update
sudo apt-get install -y \
  python3 python3-venv python3-pip git cmake build-essential \
  libgl1 libx11-6 libxrandr2 libxinerama1 libxcursor1 libxi6 \
  libxkbcommon-x11-0 xvfb
```

Build CycloneDDS C libraries from the same `0.10.2` line expected by
`unitree_sdk2_python`:

```bash
cmake -S "$UNITREE_EXTERNAL_ROOT/cyclonedds" \
  -B "$UNITREE_EXTERNAL_ROOT/cyclonedds_build" \
  -DCMAKE_INSTALL_PREFIX="$UNITREE_EXTERNAL_ROOT/cyclonedds_install" \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_EXAMPLES=OFF \
  -DBUILD_TESTING=OFF
cmake --build "$UNITREE_EXTERNAL_ROOT/cyclonedds_build" \
  --target install -j2
```

Create the external Unitree Python environment and install MissionOS into that
environment so the smoke scripts can import the repo package:

```bash
python3 -m venv "$UNITREE_EXTERNAL_ROOT/unitree_venv"
"$UNITREE_EXTERNAL_ROOT/unitree_venv/bin/python" -m pip install \
  --upgrade pip setuptools wheel

CYCLONEDDS_HOME="$UNITREE_EXTERNAL_ROOT/cyclonedds_install" \
CMAKE_PREFIX_PATH="$UNITREE_EXTERNAL_ROOT/cyclonedds_install" \
"$UNITREE_EXTERNAL_ROOT/unitree_venv/bin/python" -m pip install \
  -e "$UNITREE_EXTERNAL_ROOT/unitree_sdk2_python" \
  -e "$MISSIONOS_ROOT" \
  mujoco pygame
```

Configure the external Unitree checkout for simulation only:

```text
ROBOT = "go2"
DOMAIN_ID = 1
INTERFACE = "lo"
USE_JOYSTICK = 0
```

Then run the MissionOS smokes:

```bash
cd "$MISSIONOS_ROOT"
export PYTHONPATH="$MISSIONOS_ROOT:$UNITREE_EXTERNAL_ROOT/unitree_sdk2_python"
export LD_LIBRARY_PATH="$UNITREE_EXTERNAL_ROOT/cyclonedds_install/lib:${LD_LIBRARY_PATH:-}"

RUN_MISSIONOS_UNITREE_SDK2_IMPORT_SMOKE=1 \
UNITREE_SDK2_PYTHON_EXECUTABLE="$UNITREE_EXTERNAL_ROOT/unitree_venv/bin/python" \
"$UNITREE_EXTERNAL_ROOT/unitree_venv/bin/python" \
scripts/smoke_unitree_sdk2_import_readiness.py

RUN_MISSIONOS_UNITREE_MUJOCO_READINESS_SMOKE=1 \
UNITREE_MUJOCO_ROOT="$UNITREE_EXTERNAL_ROOT/unitree_mujoco" \
"$UNITREE_EXTERNAL_ROOT/unitree_venv/bin/python" \
scripts/smoke_unitree_mujoco_environment_readiness.py

RUN_MISSIONOS_UNITREE_MUJOCO_PROCESS_SMOKE=1 \
UNITREE_MUJOCO_ROOT="$UNITREE_EXTERNAL_ROOT/unitree_mujoco" \
UNITREE_MUJOCO_PYTHON_EXECUTABLE="$UNITREE_EXTERNAL_ROOT/unitree_venv/bin/python" \
"$UNITREE_EXTERNAL_ROOT/unitree_venv/bin/python" \
scripts/smoke_unitree_mujoco_process_launch.py
```

For a headless host or container, run the process smoke under `xvfb-run` when
the first failure is GLFW/X11 initialization:

```bash
RUN_MISSIONOS_UNITREE_MUJOCO_PROCESS_SMOKE=1 \
UNITREE_MUJOCO_ROOT="$UNITREE_EXTERNAL_ROOT/unitree_mujoco" \
UNITREE_MUJOCO_PYTHON_EXECUTABLE="$UNITREE_EXTERNAL_ROOT/unitree_venv/bin/python" \
xvfb-run -a "$UNITREE_EXTERNAL_ROOT/unitree_venv/bin/python" \
scripts/smoke_unitree_mujoco_process_launch.py
```

Observed local boundaries:

- macOS can import Unitree SDK2 from a Python 3.12 external environment, but
  Unitree's simulator bridge exits early before viewer initialization because it
  imports Linux-only `timerfd_create`.
- Ubuntu Docker with Linux-built CycloneDDS and SDK2 clears the `timerfd`
  boundary. A minimal headless container then exits early at GLFW/X11
  initialization unless X11 or Xvfb support is provided.
- Ubuntu Docker with headless MuJoCo direct scene loading has observed
  `scene_loaded_observed=true` and `robot_motion_observed=true` for the Go2
  scene. That is passive simulation evidence, not MissionOS dispatch evidence.
- Unitree SDK2 DDS initialization with an explicit interface such as `lo` or
  `eth0` aborted in the local Docker environment. In a `--network=none`
  container, SDK2 DDS autodetermine selected loopback and allowed a headless
  `UnitreeSdk2Bridge` to publish `rt/lowstate`. The MissionOS bridge
  observation smoke now enforces this isolation rule before accepting `auto`.
  Treat that as an isolated Docker observation path only; do not generalize DDS
  autodetermine to hosts that can see a real robot network.
- A high-level `SportClient.Move(0.1, 0, 0)` probe against the headless bridge
  returned a send error because the upstream MuJoCo bridge publishes low-level
  state and subscribes `rt/lowcmd`; it does not provide the Go2 sport service.
  The MissionOS SportClient probe records this as `probe_status=blocked`,
  `dispatch_request_sent=false`, and `raw_lowcmd_published=false`. MissionOS
  therefore still has no verified raw-motor-free bounded local move dispatch
  for Unitree MuJoCo.
- The MissionOS SportClient bridge command can be wired into the bounded
  dispatch smoke. In the current upstream bridge it reaches the same high-level
  SportClient boundary and records `ack_status=rejected`, no command
  completion, `sport_server_api_code=3102`,
  `robot_state_service_list_code=3102`, and `raw_lowcmd_published=false`.
- The passive probes must keep `dispatch_request_sent=false`. The bounded
  dispatch smoke may set `dispatch_request_sent=true` when it sends a request to
  the operator-owned bridge, but a rejected ACK must not become completion and
  must keep `physical_execution_invoked=false`.

## Not A Bounded Move Yet

This readiness slice is not the same as executing a bounded local move. It only
proves that the external simulator checkout appears compatible with the
MissionOS Unitree adapter contract.

The headless scene observation smoke proves that MuJoCo can load the Go2 scene
and that physics can move the model. It still does not prove MissionOS control.
The headless SDK2 bridge observation proves that Unitree DDS state publication
can be brought up in an isolated Linux container. It still does not prove
bounded dispatch.

The SportClient bounded-move probe proves the next boundary: MissionOS can
attempt the official high-level Go2 SDK2 move surface in the same isolated
simulator setup. In the current upstream bridge, that probe is expected to block
because the bridge does not serve the Go2 sport service.

The SportClient bridge command is the first operator-bridge implementation for
the existing `UnitreeMujocoBridgeCommandClient`. It attempts only the official
high-level Go2 SDK2 surface, so a rejected ACK is not a cue to publish raw
`rt/lowcmd`.

The upstream `test_unitree_sdk2.py` and C++ `test` examples are not MissionOS
bounded-action smokes. The upstream README says those tests continuously output
1Nm of torque for each motor. Do not treat those examples as `bounded_local_move`
evidence.

Do not implement `bounded_local_move` by publishing `rt/lowcmd` from MissionOS.
That would be raw motor control and remains outside this adapter slice.

## Next Runtime Slice

The next runtime slice should add an opt-in client wrapper that connects to the
already-running simulator and maps a bounded MissionOS action to the
`UnitreeSimClient` protocol:

```text
MissionOS approval
  -> UnitreeHardwareAdapter
  -> UnitreeSimClient implementation
  -> already-running Unitree MuJoCo simulation
  -> ACK / state / progress evidence
  -> completion_scope=sim_action
  -> physical_execution_invoked=false
```

That future slice must keep `raw_motor`, `raw_velocity`, and `special_motion`
blocked.

The command bridge now provides this client-wrapper boundary, but the actual
Unitree SDK2/MuJoCo bridge command remains operator-owned and opt-in. MissionOS
still does not ship raw motor, velocity, or special-motion control code.
