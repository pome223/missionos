# Integrated MissionOS Assurance graph

This release combines the PR #103/#104 Prediction and governed execution
implementation with the proposal graph. Model terminology follows the correction
in PR #107; the stacking component is a task-specific ExtraTrees predictor. The active roles
are the seven [proposal agents](seven-agent-graph.md) plus Mission Assurance.
Root, Dialogue Router, and Knowledge Curator remain omitted.

## Execution boundaries

The proposal workflow routes Chief → specialist → Safety Critic. Operational
Recovery enters the separate ADK Mission Incident workflow:

```text
Observation → Runtime Recovery → Rules: candidate feasibility
  → Prediction evidence admission → Mission Assurance → approval checkpoint
  → explicit human approval / approved bounded policy
  → dispatch-time Rules revalidation → Executor → Verifier → next observation
```

Judgment and post-approval continuation are two linked ADK workflows. The second
binds the hash of the frozen first result; it never treats a model choice as
approval. A queued executor request is not an observed effect or mission success.
The deployed topology lists both workflows, rather than representing every role
as an LLM node. Prediction, Rules, Executor and Verifier are distinct components.

`POST /missionos/mission-incident/run` runs the actual judgment workflow on
caller-supplied input, for diagnostics only. It cannot create a TaskStore approval
candidate. Operational `/missionos/runtime-recovery-agent/propose-for-task` uses
the task's observation evidence and persists the frozen checkpoint. Existing
approval and execution routes then run the continuation workflow.

An observation owner may supply these task artifacts:

- `missionos_prediction_evidence`: the generic forecast envelope.
- `missionos_prediction_context`: independently captured current execution,
  observation, revision, source and model/policy binding.
- `missionos_prediction_contract`: the declared prediction mission contract.

The caller's forecast must not establish its own current context. At least one
forecast option must match the Recovery action and exact parameters. Missing
optional Prediction evidence is recorded as `not_supplied`. Supplied but rejected
evidence stops this graph before Assurance. The accepted envelope digest, current
owner context, and 30-second freshness limit are checked again at dispatch.
Changes require a fresh judgment and checkpoint; no silent migration of old
approval checkpoints is performed.

The stacking adapter retains the separate PR #104 loopback service and explicit
trusted-checkpoint/seed policy configuration. `prediction serve-stacking-mission`
runs Prediction → admission → Assurance → `/dispatch` revalidation → external
simulator Executor → `/observe` verification. It is an HTTP state machine, not an
ADK Workflow. The eight-role Gateway alone does not start a prediction model, VLA
server, simulator, or hardware. Model health is `not_probed` until actually tested.

## Jev placement

`MISSIONOS_JEV_MODE` supports `off` (default), `shadow`, `primary`, and the
experimental `cascade` / `cascade_shadow` modes:

```dotenv
MISSIONOS_JEV_MODE=off
# Use shadow for comparison, or primary for experimental Jev judgment.
# TYPESAFE_API_KEY is needed only when Jev is enabled (or supplied by Secret Manager).
```

The release launcher resolves this setting in order: explicit `--jev-mode`,
process environment, state directory `.env`, then `off`. Invalid values stop the
launcher before any secret lookup. Restart the Gateway to apply changes.
This switch controls the configured Mission Assurance judge; the stacking lab
CLI has its separate explicit `--llm-backend` selection.

- `shadow`: run Jev alongside the configured Assurance judge on the same prompt.
  Only the primary output controls the proposal. Disagreement, distribution,
  latency and failure type are attached to its invocation evidence.
- `primary`: opt-in experimental bounded response selection by Jev. Invalid
  responses and provider failures escalate. Approval and Rules remain mandatory.

The original three modes ask two independent questions in one request: the bounded mission response
and whether evidence needs further review. Review/confidence do not bypass Rules
or select a fallback automatically. Probability and confidence are provider model
outputs, not calibrated mission-success probabilities. Jev does not write a
rationale; adapter-generated explanatory strings are explicitly labeled templates.

[Cascade routing](jev-assurance-cascade.md) adds a third question to distinguish
bounded judgment, extra reasoning, missing observations, and human review.
`cascade_shadow` preserves the incumbent output. `cascade` uses an explicit
routing policy; its Jev-only fast path is disabled by default and currently has
only an opt-in fixture profile. No live applicability is inferred from the pilot.

`TYPESAFE_API_KEY` is read from the process environment, never recorded in
artifacts. `run_agent_graph_gateway.py --jev-mode shadow` reads it from the supplied
Secret Manager project (`--jev-secret-name`, default `jev-api-key`) and also enables
Mission Assurance. Use `--jev-mode off` to remove the comparison without changing
the primary Assurance implementation. Stacking supports `--llm-backend jev
--llm-model jev-latest` under its existing simulator opt-in authorization contract.

## Runtime verification

With Core, CLI and Gateway on PYTHONPATH:

```sh
python scripts/smoke_operator_chat_mission_incident_graph.py
python scripts/smoke_assurance_prediction_evidence.py
RUN_MISSIONOS_JEV_ASSURANCE_SMOKE=1 python scripts/smoke_jev_assurance.py --output output/jev-paired
```

`python scripts/smoke_jev_modes_gateway.py` additionally checks all three modes
through real Gateway HTTP and ADK graphs using fixture model responses. It tests
that a disagreeing Jev response cannot change the primary decision in shadow mode.

The first command uses a real Gateway HTTP client, ADK workflows, TaskStore,
explicit fixture approval and fixture executor queue. The second checks evidence
admission through the CLI. The third makes hosted Jev/DeepSeek calls through the
PR #104 HTTP decision/dispatch/observe service on synthetic numeric inputs. Its
simulator-shaped observation messages are fixtures, not simulator measurements.
No physical or simulator outcome improvement can be inferred. Keep experimental
provider comparisons local; do not publish generated artifacts automatically.

## Native navigation prediction

PX4 route deviations and TurtleBot3 recovery checkpoints can invoke an explicitly
configured navigation WAM after candidate feasibility and before Assurance.
The [navigation WAM/Jev contract](navigation-wam-jev.md) defines the independent
off/shadow/required switch, HTTP model binding, and native dispatch revalidation.
No trained navigation checkpoint is bundled; fixture transport checks are not
learned prediction or live simulator evidence.
