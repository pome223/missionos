"""Verifier agent for the ADK-backed v2 control loop."""

from google.adk.agents import LlmAgent

from src.agents.model_config import DEFAULT_MODEL
from src.control_loop.instructions import build_verifier_instruction
from src.runtime.state_keys import StateKeys


verifier_agent = LlmAgent(
    name="verifier",
    model=DEFAULT_MODEL.name,
    instruction=build_verifier_instruction,
    output_key=StateKeys.VERIFY_LAST_REPORT,
    description=(
        "Evaluates execution results against success criteria. "
        "Reads plan:approved and temp:executor_outputs, writes verify:last_report."
    ),
)
