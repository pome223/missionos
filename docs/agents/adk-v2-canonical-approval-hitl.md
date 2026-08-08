# ADK v2 Canonical Approval HITL Contract

This contract defines how MissionOS may use ADK v2 `RequestInput` to pause and
resume orchestration around an existing Form 2A human approval. ADK transports
the response; it does not create, infer, consume, or broaden approval.

## Authority Boundary

The authoritative sequence is:

```text
MissionOS creates a bounded Form 2A response selection and approval token
-> ADK stores the exact immutable binding and pauses with RequestInput
-> a human uses the existing MissionOS operator-review approval route
-> the client resumes with the exact canonical approval_ref
-> MissionOS reloads and validates the current artifacts
-> the graph reports canonical_approval_validated or approval_blocked
```

Submitting `yes`, `approved`, or any other unbound response is not approval.
The resume route requires the exact `approval_ref` stored at the checkpoint.
The post-resume validator then reloads the artifacts and verifies:

- the current human-review summary is `approved`
- the approval token is still issued, unconsumed, and unexpired
- `approval_ref` matches the checkpoint and current review
- `mission_response_candidate_ref` is unchanged
- the proposal artifact SHA-256 is unchanged
- `bounded_action_ref` and `dispatch_ref` are unchanged

The HITL workflow never calls the Form 2A approval route itself. It also does
not consume the approval token, claim a dispatch, call an executor, observe an
effect, pass a verifier, or count mission progress.

## Runtime Surfaces

The feature is opt-in:

```text
MISSIONOS_ADK_V2_HITL_ENABLED=1
REDIS_URL=redis://127.0.0.1:6379/0
```

Both routes fail closed unless `create_session_service(..., require_redis=True)`
selects the Redis backend:

- `POST /missionos/adk-v2/hitl/form2a-approval/start`
  - request: `operator_session_id`
  - response: `adk_session_id`, `interrupt_id`, and the bound canonical refs
- `POST /missionos/adk-v2/hitl/form2a-approval/resume`
  - request: `operator_session_id`, `adk_session_id`, and exact `approval_ref`
  - response: fresh validation result or explicit blocking reasons

The human approval remains on the pre-existing surface:

```text
POST /missionos/form2a-operator-review/approve
```

## Resume Policy

The pure binding node and pause node use `rerun_on_resume=false`; their values
are the checkpointed expectation, not fresh authority. The canonical artifact
validator and finalizer use `rerun_on_resume=true`. Every resume therefore
reloads the current selection, hash, token expiry, and operator-review state.

The final result fixes these facts unless a later, separate MissionOS boundary
establishes them:

- `approval_created=false`
- `dispatch_authority_created=false`
- `executor_invoked=false`
- `physical_execution_invoked=false`
- `outcome_observed=false`
- `progress_counted=false`

`canonical_approval_validated=true` means only that a currently valid,
human-created MissionOS approval was revalidated. It is not dispatch authority.

## Runtime Verification

Run the maintained Redis checkpoint test with a disposable Redis instance:

```text
REDIS_URL=redis://127.0.0.1:6379/0 \
REDIS_SESSION_NAMESPACE=missionos:adk-v2:hitl-smoke \
PYTHONPATH=packages/missionos-cli/src:. \
python scripts/verify_adk_v2_hitl_redis_resume.py
```

The script starts and resumes in different Python processes, verifies an exact
canonical-ref response, verifies that `yes` does not attempt resume, and
deletes both fixture sessions. Its validator is a public-safe fixture; it
proves the ADK/Redis transport boundary but not a live Form 2A approval or any
execution.

The real Gateway HTTP boundary, including the existing human-review route and
fresh canonical artifact validation, is exercised with:

```text
REDIS_URL=redis://127.0.0.1:6379/0 \
REDIS_SESSION_NAMESPACE=missionos:adk-v2:hitl-gateway-smoke \
PYTHONPATH=packages/missionos-cli/src:. \
python scripts/smoke_adk_v2_hitl_gateway.py
```

This second smoke starts the production MissionOS Gateway on loopback, uses a
temporary public-safe Form 2A fixture, and calls all three real HTTP routes. It
still proves no simulator, hardware, dispatch, executor, observed effect, or
mission completion.
