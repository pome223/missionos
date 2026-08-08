# ADK v2 Redis Session Persistence Contract

This contract defines the persistence evidence required before MissionOS can
enable ADK v2 workflow restart or resume. Redis stores ADK orchestration state;
it does not become MissionOS approval, dispatch, execution, verifier, or audit
truth.

## Backend Selection

Normal MissionOS operation may use `InMemorySessionService` when `REDIS_URL` is
empty. A restart/resume verification must instead call:

```python
create_session_service(settings, require_redis=True)
```

The verification must fail unless both conditions hold:

- `REDIS_URL` is non-empty after trimming whitespace
- `describe_session_backend(settings)` returns `backend=redis`

A test that silently exercises the memory fallback is not Redis persistence
evidence.

## Required ADK v2 Event Fields

`RedisSessionService` serializes the complete ADK `Session` model. Restart
verification must round-trip at least:

- event `id`, `invocation_id`, and `author`
- `node_info.path`
- `node_info.output_for`
- typed node `output`
- `custom_metadata.missionos_correlation`
- `actions.state_delta`
- session state and event ordering

The correlation metadata should carry stable MissionOS identifiers when they
exist:

- `task_id`
- workflow name and node path
- `mission_response_candidate_ref`
- `approval_ref`
- `bounded_action_ref`
- `dispatch_ref`
- receipt and verifier refs

Missing canonical refs remain absent. An ADK event ID must not be substituted
for any MissionOS artifact ref.

## Process-Restart Verification

The maintained verification entrypoint is:

```text
scripts/verify_adk_v2_redis_persistence.py
```

`--mode roundtrip` requires a real `REDIS_URL` and performs these steps:

1. a writer Python process creates the session and appends an ADK v2 event
2. the writer exits
3. a separate reader Python process constructs a new Redis client and restores
   the session
4. the reader validates node metadata, node output, custom correlation
   metadata, state delta, and the MissionOS authority floor
5. a separate cleanup process deletes the fixture session

The result is valid only when writer and reader PIDs differ and the reported
backend is `redis`.

## Resume Is Not Authority

Restoring an event or checkpoint proves only that orchestration data survived
the process boundary. Before any resumed execution node, MissionOS must reload
and revalidate:

- the current proposal and canonical approval envelope
- approval expiry and proposal/hash binding
- fresh telemetry and external state
- backend capability and safety descriptors
- existing `dispatch_ref` claims and receipts
- the current deterministic policy and gate result

Telemetry-dependent Chief, specialist, and Safety Critic nodes must rerun on
fresh checkpoint-bound input. Cached pre-pause LLM output is not
dispatch-time truth.

Node completion, session restoration, and `RequestInput` response values do not
create approval, dispatch authority, sender invocation, ACK, observed effect,
verifier passage, completion, physical execution, or mission progress.

## Operational Limits

- Redis availability is required only when persistence/resume is explicitly
  enabled; normal memory fallback remains available for rollback.
- The current verification stores proposal-only fixture output and invokes no
  simulator, hardware, or external executor.
- Dispatch and hardware-affecting writes remain outside ADK automatic retry.
- Production resume remains disabled until canonical HITL approval and fresh
  dispatch-time revalidation are implemented.
