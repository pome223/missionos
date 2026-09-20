# Seven-agent proposal graph

The active registry contains Chief, Situation Judge, Response Planner, Runtime
Recovery, Flight Scenario Designer, Repair Planner, and Safety Critic.
Root, Dialogue Router, and Knowledge Curator are omitted from the active registry
and Chief instructions. Compatibility builder functions remain in source.

ADK v2 executes normalize → Chief → selected specialist → Safety Critic → finalize.
The specialist is selected from the Chief intent by a deterministic table. Actual
LLM calls are dynamic Workflow children invoked through `ctx.run_node`; standalone
nested Runners are not used in this proposal graph. It is a bounded workflow,
not peer-to-peer agent negotiation. Runtime Recovery's proposal node does not
dispatch; its existing tool-backed operational recovery path remains separate.

The graph returns proposals only. Approval, dispatch, Executor, and Verifier
remain separate boundaries. A failed primary graph stops the conversation request
before legacy Dialogue Router or keyword action fallback. Explicit rollback uses
the existing sequential engine.

`GET /health` includes configured execution mode, active agents, omitted roles,
and deployment revision. `llm_health=not_probed` deliberately distinguishes process
health from model availability. `GET /missionos/agent-runtime` also returns the
latest recorded graph status and node paths; this is historical evidence, not a
fresh model probe. Both use the existing Gateway authentication boundary.

Each graph receipt under `output/mission_designer_behavior_delta_audits/missionos_agent_graph/`
has a unique `graph_run_id`, full node paths, and child invocation evidence with
the same ID. These local outputs must not be committed as public fixtures.

## Verification

Set PYTHONPATH to the repository and the `src` directories of the Core, CLI and
Gateway packages, or install all three packages in the active virtual environment.

```sh
python -m pytest tests/contract/test_missionos_adk_v2_shadow_graph.py tests/contract/test_seven_agent_graph.py tests/contract/test_missionos_adk_v2_hitl.py tests/contract/test_missionos_adk_v2_guarded_execution.py tests/contract/test_missionos_adk_v2_recovery.py -q
REDIS_URL=redis://127.0.0.1:16389/0 python scripts/smoke_adk_v2_guarded_execution_gateway.py
REDIS_URL=redis://127.0.0.1:16389/1 python scripts/smoke_adk_v2_hitl_gateway.py
RUN_MISSIONOS_SEVEN_AGENT_GRAPH_SMOKE=1 python scripts/smoke_seven_agent_graph.py --output output/seven-agent-live
RUN_MISSIONOS_ADK_V2_PRIMARY_GATEWAY_SMOKE=1 python scripts/smoke_adk_v2_primary_gateway.py
```

The HITL commands require a disposable local Redis instance at the supplied URL.
The last two commands use the configured hosted LLM and require credentials in
the process environment. Synthetic proposal inputs verify the five specialist
branches and all seven roles. The primary Gateway smoke checks a real loopback
HTTP request and actual ADK child nodes. HITL/guarded-execution smokes use a fixture
executor; they do not establish simulator or physical mission success.

## Local deployment

Use `scripts/run_agent_graph_gateway.py` with an explicit state directory, task
database, Secret Manager project, and port. It reads the DeepSeek credential into
memory and preserves operator-owned `.env` configuration from the state directory.
Default startup is planning-only; `--enable-live-sitl` preserves an explicitly
authorized simulator deployment. No simulator action is initiated by startup.

Before replacing a listener, check that no task is running, back up the SQLite
task database using its backup API, and save the previous launch configuration.
After replacement, verify `/health`, `/missionos/agent-runtime`, a real status-only
conversation request, and the pre-existing task IDs. Roll back to the saved prior
launch if any required check fails. Chat sessions in the in-memory session service
do not survive a process restart; persistent task records do.

The launcher runs in the foreground and is suitable for a local process manager.
It does not install a boot service or change other Gateway ports.
