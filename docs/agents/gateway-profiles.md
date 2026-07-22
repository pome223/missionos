# Gateway Profiles

MissionOS has two explicit production-capable Gateway profiles. They are
separate route surfaces, not middleware visibility modes.

## MissionOS profile

`create_missionos_gateway()` is the production entrypoint used by
`MISSIONOS_GATEWAY_BACKEND=production`. Its route table contains MissionOS
planning, approval, task, evidence, simulator, and recovery boundaries.

The following legacy prefixes are absent from the route table and OpenAPI
document:

- `/agent`
- `/control-loop`
- `/tools`
- `/skills`
- `/runtime`
- `/sessions`
- `/transcript`
- `/memory`
- `/subagents`
- `/cron`
- `/ws`

A request to one of these paths receives FastAPI's normal `404` because there
is no registered handler. Authentication middleware is not used to conceal a
handler.

Legacy control-loop mutations under the task namespace are also excluded:

- `POST /tasks/{task_id}/replay`
- `POST /tasks/supervisors/control-loop`
- `POST /tasks/{task_id}/cancel`

Read-only task, timeline, analytics, and comparison routes remain available.
Production construction does not import the legacy root agent, subagents,
shell/browser tools, memory tools, cron scheduler, control-loop runtime, or
legacy WebSocket handler.

## Legacy agent profile

`create_legacy_agent_gateway()` is an explicit compatibility boundary for the
old general-agent application. `MISSIONOS_GATEWAY_BACKEND=legacy-agent` selects
it. It registers the legacy HTTP and WebSocket routes and must not be used as a
MissionOS production backend.

`create_gateway()` remains temporarily for old internal imports. New production
entrypoints must use one of the named factories so route authority cannot be
changed accidentally by middleware behavior.

The profile split does not change claim authority. In both profiles, an LLM
proposal is not human approval, dispatch, execution, verification, delivery
completion, or physical execution.
