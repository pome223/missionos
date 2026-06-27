"""Simulator adapter contract — design slice (#171).

This module defines ``simulator_adapter_contract.v1``, the static description
each simulator adapter publishes about itself. It is intentionally **artifact
only**: this slice does not route any runtime through an adapter interface,
does not connect to PX4 SITL / Gazebo / AirSim / Isaac Sim, and does not open
any path to live or physical execution.

What the contract is for
------------------------

When we add a second simulator (or a hardware-in-the-loop telemetry stream),
we want a single artifact that declares:

- which schema versions the adapter speaks (state / action / telemetry /
  governor / episode / replay trace)
- whether the adapter supports stronger execution modes (it must not, in
  this slice — the booleans are pinned to ``False`` at the type level)
- whether operator approval is required (always ``True``)
- the adapter's high-level mode (``dry_run_only`` for now)

Future PRs can:

- have ``run_toy_grid_world_autonomous_episode`` consult the contract before
  consenting to step the simulator
- gate ``autonomy_gate_result`` / ``autonomy_gate_comparison_result`` on the
  contract advertising the same simulator the artifacts came from
- introduce ``hardware_in_the_loop_telemetry_only`` / ``simulated_only`` /
  ``limited_live_execution`` modes by adding new ``SimulatorAdapterMode``
  values, with their own ``Literal`` invariants

Out of scope for this PR
------------------------

- Routing toy-grid runtime through the contract
- Adding a second adapter
- Live / physical / ROS dispatch paths
- Mission API / promotion / runtime reuse
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


SIMULATOR_ADAPTER_CONTRACT_SCHEMA_VERSION = "simulator_adapter_contract.v1"
MOCK_PHYSICAL_SIMULATOR_ADAPTER_ID = "mock_physical_simulator.v1"
MOCK_PHYSICAL_SIMULATOR_KIND = "mock_physical_simulator"
MOCK_PHYSICAL_STATE_SCHEMA_VERSION = "mock_physical_simulator_state.v1"
MOCK_PHYSICAL_ACTION_SCHEMA_VERSION = "mock_physical_simulator_action.v1"
MOCK_PHYSICAL_TELEMETRY_SCHEMA_VERSION = "telemetry_health_snapshot.v1"
MOCK_PHYSICAL_GOVERNOR_SCHEMA_VERSION = "safety_governor_decision.v1"
MOCK_PHYSICAL_EPISODE_SCHEMA_VERSION = "mock_physical_simulator_episode.v1"
MOCK_PHYSICAL_REPLAY_TRACE_SCHEMA_VERSION = (
    "mock_physical_simulator_replay_trace.v1"
)


class SimulatorAdapterMode(str, Enum):
    """The execution mode an adapter advertises.

    Only ``dry_run_only`` exists today. Stronger modes (HIL telemetry-only,
    limited live execution) are deliberately not added until #172 / #173 land
    with their own approval and policy story.
    """

    DRY_RUN_ONLY = "dry_run_only"


class SimulatorAdapterContract(BaseModel):
    """Static description of a single simulator adapter.

    The boolean capability fields are pinned to ``False`` via ``Literal`` so
    Pydantic refuses to construct a contract that advertises live, physical,
    or ROS-dispatch capabilities through this slice. Any future adapter that
    needs those capabilities must come with its own contract version (v2+)
    and an explicit approval / policy story.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[SIMULATOR_ADAPTER_CONTRACT_SCHEMA_VERSION] = (
        SIMULATOR_ADAPTER_CONTRACT_SCHEMA_VERSION
    )
    adapter_id: str
    simulator_kind: str
    state_schema: str
    action_schema: str
    telemetry_schema: str
    governor_schema: str
    episode_schema: str
    replay_trace_schema: str
    supports_live_execution: Literal[False] = False
    supports_physical_execution: Literal[False] = False
    supports_ros_dispatch: Literal[False] = False
    operator_approval_required: Literal[True] = True
    adapter_mode: Literal[SimulatorAdapterMode.DRY_RUN_ONLY] = (
        SimulatorAdapterMode.DRY_RUN_ONLY
    )


class SimulatorAdapterContractError(ValueError):
    """Raised when a simulator adapter contract fails validation against the
    expected adapter for the runtime path being entered.
    """


def normalize_simulator_adapter_contract(
    contract: SimulatorAdapterContract | dict[str, Any],
) -> SimulatorAdapterContract:
    """Parse a simulator adapter contract and wrap validation errors.

    The Pydantic model remains the first safety line: unknown fields such as
    ``supports_mavlink_dispatch`` or ``supports_actuator_execution`` are
    rejected by ``extra='forbid'`` rather than silently ignored.
    """

    if isinstance(contract, SimulatorAdapterContract):
        return contract
    try:
        return SimulatorAdapterContract.model_validate(contract)
    except Exception as exc:  # ValidationError or TypeError
        raise SimulatorAdapterContractError(
            f"simulator_adapter_contract failed Pydantic validation: {exc}"
        ) from exc


def validate_simulator_adapter_safety_compatibility(
    contract: SimulatorAdapterContract | dict[str, Any],
) -> SimulatorAdapterContract:
    """Validate the common adapter-backed simulator safety boundary.

    This helper is intentionally generic across the current toy-grid and mock
    adapter contracts. It does not prove simulator correctness; it proves the
    adapter declaration is compatible with the current Mission OS safety
    boundary before a runtime or smoke chain may consume it.
    """

    validated = normalize_simulator_adapter_contract(contract)
    if validated.supports_live_execution is not False:
        raise SimulatorAdapterContractError(
            "simulator_adapter_contract.supports_live_execution must be false"
        )
    if validated.supports_physical_execution is not False:
        raise SimulatorAdapterContractError(
            "simulator_adapter_contract.supports_physical_execution must be false"
        )
    if validated.supports_ros_dispatch is not False:
        raise SimulatorAdapterContractError(
            "simulator_adapter_contract.supports_ros_dispatch must be false"
        )
    if validated.operator_approval_required is not True:
        raise SimulatorAdapterContractError(
            "simulator_adapter_contract.operator_approval_required must be true"
        )
    if validated.adapter_mode is not SimulatorAdapterMode.DRY_RUN_ONLY:
        raise SimulatorAdapterContractError(
            "simulator_adapter_contract.adapter_mode must be dry_run_only"
        )
    schema_refs = {
        "state_schema": validated.state_schema,
        "action_schema": validated.action_schema,
        "telemetry_schema": validated.telemetry_schema,
        "governor_schema": validated.governor_schema,
        "episode_schema": validated.episode_schema,
        "replay_trace_schema": validated.replay_trace_schema,
    }
    missing = [name for name, value in schema_refs.items() if not str(value).strip()]
    if missing:
        raise SimulatorAdapterContractError(
            "simulator_adapter_contract is missing schema refs: "
            + ", ".join(sorted(missing))
        )
    return validated


def build_mock_physical_simulator_adapter_contract() -> SimulatorAdapterContract:
    """Return a deterministic second simulator contract fixture.

    This fixture is deliberately not wired to runtime execution. It exists to
    prove that ``simulator_adapter_contract.v1`` can describe a non-toy-grid
    simulator-like surface while preserving the current safety boundary:
    dry-run-only, operator-approval-required, and no live / physical / ROS
    capability.
    """

    return SimulatorAdapterContract(
        adapter_id=MOCK_PHYSICAL_SIMULATOR_ADAPTER_ID,
        simulator_kind=MOCK_PHYSICAL_SIMULATOR_KIND,
        state_schema=MOCK_PHYSICAL_STATE_SCHEMA_VERSION,
        action_schema=MOCK_PHYSICAL_ACTION_SCHEMA_VERSION,
        telemetry_schema=MOCK_PHYSICAL_TELEMETRY_SCHEMA_VERSION,
        governor_schema=MOCK_PHYSICAL_GOVERNOR_SCHEMA_VERSION,
        episode_schema=MOCK_PHYSICAL_EPISODE_SCHEMA_VERSION,
        replay_trace_schema=MOCK_PHYSICAL_REPLAY_TRACE_SCHEMA_VERSION,
        adapter_mode=SimulatorAdapterMode.DRY_RUN_ONLY,
    )


__all__ = [
    "MOCK_PHYSICAL_ACTION_SCHEMA_VERSION",
    "MOCK_PHYSICAL_EPISODE_SCHEMA_VERSION",
    "MOCK_PHYSICAL_GOVERNOR_SCHEMA_VERSION",
    "MOCK_PHYSICAL_REPLAY_TRACE_SCHEMA_VERSION",
    "MOCK_PHYSICAL_SIMULATOR_ADAPTER_ID",
    "MOCK_PHYSICAL_SIMULATOR_KIND",
    "MOCK_PHYSICAL_STATE_SCHEMA_VERSION",
    "MOCK_PHYSICAL_TELEMETRY_SCHEMA_VERSION",
    "SIMULATOR_ADAPTER_CONTRACT_SCHEMA_VERSION",
    "SimulatorAdapterContract",
    "SimulatorAdapterContractError",
    "SimulatorAdapterMode",
    "build_mock_physical_simulator_adapter_contract",
    "normalize_simulator_adapter_contract",
    "validate_simulator_adapter_safety_compatibility",
]
