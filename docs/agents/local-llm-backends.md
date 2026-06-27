# Local LLM Backends

MissionOS is designed around an LLM-in-the-loop chat path. LLM-backed ADK paths
must be selected explicitly: Gemini for the fastest hosted API path, or
Ollama/Gemma for a local no-spend path that is slower and role-dependent.

`MISSIONOS_LLM_BACKEND=off` is a development fallback for boundary tests. It is
not the intended public product experience.

## Backend Selection

Global backend:

```bash
MISSIONOS_LLM_BACKEND=off      # development fallback, no LLM-backed ADK paths
MISSIONOS_LLM_BACKEND=gemini   # Google ADK/Gemini opt-in
MISSIONOS_LLM_BACKEND=ollama   # local Ollama through ADK LiteLLM
```

Default local model when `MISSIONOS_LLM_BACKEND=ollama` is selected:

```bash
MISSIONOS_OLLAMA_MODEL=gemma4:26b
MISSIONOS_OLLAMA_BASE_URL=http://localhost:11434
```

When `MISSIONOS_LLM_BACKEND=off`, CLI-managed Gateway child processes disable
LLM-backed ADK paths and use deterministic fallbacks where available. Use this
for boundary tests, not as the main MissionOS chat experience.

When `MISSIONOS_LLM_BACKEND=ollama`, CLI-managed Gateway child processes do not
receive `GOOGLE_API_KEY`, even when it is present in the parent environment or
`.env`.

If a deployment previously relied on `GOOGLE_API_KEY` being inherited by Gateway
children, set `MISSIONOS_LLM_BACKEND=gemini` explicitly before restarting
Gateway. Otherwise MissionOS intentionally keeps the Google key out of the child
process and ADK paths either stay disabled or use the selected local backend.

`MISSIONOS_LLM_DIALOGUE_ROUTER_MODEL_ID` is a Gemini/API model id override. With
the Ollama backend it is not the effective local selector. Use
`MISSIONOS_AGENT_MISSIONOS_DIALOGUE_ROUTER_AGENT_OLLAMA_MODEL`,
`MISSIONOS_AGENT_MISSIONOS_DIALOGUE_ROUTER_AGENT_LOCAL_MODEL`, or the global
`MISSIONOS_OLLAMA_MODEL` / `MISSIONOS_LOCAL_MODEL` instead.

## Local Timeout Settings

Local models can be much slower than hosted Gemini, especially on the first
request while Ollama loads the model. Increase the bounded ADK timeouts in
`.env` before restarting Gateway only when intentionally validating an Ollama
path:

```bash
MISSIONOS_LLM_DIALOGUE_ROUTER_TIMEOUT_SECONDS=180
MISSIONOS_CHIEF_ROUTE_SEMANTIC_TIMEOUT_SECONDS=240
MISSIONOS_AGENT_RUNTIME_TIMEOUT_SECONDS=240
MISSIONOS_LLM_REPAIR_PLANNER_TIMEOUT_SECONDS=180
MISSIONOS_LLM_RESPONSE_PLANNER_TIMEOUT_SECONDS=180
MISSIONOS_REAL_HARDWARE_ARM_DISARM_PLANNER_TIMEOUT_SECONDS=180
```

After changing these values, restart Gateway before validating `missionos chat`,
`missionos operate`, repair, or live SITL behavior.

## Per-Agent Overrides

Agent-specific settings use the sanitized ADK agent name:

```bash
MISSIONOS_AGENT_MISSIONOS_CHIEF_AGENT_LLM_BACKEND=ollama
MISSIONOS_AGENT_MISSIONOS_CHIEF_AGENT_OLLAMA_MODEL=gemma4:26b

MISSIONOS_AGENT_MISSIONOS_RUNTIME_RECOVERY_AGENT_LLM_BACKEND=gemini
MISSIONOS_AGENT_MISSIONOS_RUNTIME_RECOVERY_AGENT_MODEL_ID=gemini-3.1-flash-lite-preview
```

The global backend remains the fallback. Per-agent overrides are for deliberate
exceptions, not for approval, dispatch, execution, or verifier authority.

## Verified Role Split

Past local Gemma 4 26B MoE verification showed different results by role:

| Role/path | Result |
| --- | --- |
| Chief / planning function-tool path | Worked locally with Gemma 4 26B MoE for route and condition extraction when client and ADK timeouts were extended. |
| Live Runtime Recovery loop | Not recommended as a default or for repeated in-flight use: observed high latency and fragile JSON-mode output. |
| JSON-mode planners | Treat as unproven unless validated for the exact prompt and guardrail path. |

This is not a blanket "local LLM works" or "local LLM does not work" result.
MissionOS roles have different contracts. Tool-calling planning and live
telemetry recovery are different workloads.

For local models, JSON-mode outputs may include preamble text or markdown code
fences. The Dialogue Router may salvage a syntactically valid JSON object from
that response, but the existing guardrail still decides whether the proposal is
usable. Salvage never creates approval, dispatch authority, execution, or
progress.

## Authority Boundary

Changing model backend never changes authority:

```text
LLM judges.
Human approves.
Rules constrain.
Executor acts.
Verifier checks.
Repair loops.
```

An Ollama/Gemma proposal is still only a proposal. A Gemini proposal is also
only a proposal. Gateway owns approval records, bounded parameter checks,
dispatch authority, runtime evidence, and verifier truth.
