"""MissionOS claim boundary constants."""

AUTHORITY_SPLIT = (
    "LLM judges.",
    "Human approves.",
    "Rules constrain.",
    "Executor acts.",
    "Verifier checks.",
    "Repair loops.",
)

CLAIM_BOUNDARY_ORDER = (
    "proposal_created",
    "approval_recorded",
    "dispatch_authority_created",
    "dispatch_request_sent",
    "command_ack_observed",
    "runtime_progress_observed",
    "landing_observed",
    "delivery_completion_claimed",
    "physical_execution_invoked",
)
