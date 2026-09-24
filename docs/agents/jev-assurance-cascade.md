# Experimental Jev routing inside Mission Assurance

An Agent remains a mission role. Its configured judge may use Jev for a bounded
response and the configured Assurance LLM for additional reasoning. The reasoner
is normally DeepSeek, but the cascade does not hard-code a provider. Other agents
are unchanged.

## Configuration

`MISSIONOS_JEV_MODE` additionally accepts:

- `cascade_shadow`: evaluate the cascade alongside the incumbent. Only the
  incumbent output is returned. Its one model call is reused if the candidate
  cascade needs reasoning, so there is no duplicate incumbent call. This mode
  measures routing disagreements, not latency or cost savings from skipping the
  incumbent.
- `cascade`: Jev runs first. Code chooses whether to use its response, invoke the
  configured reasoner, request observations, or request human review.

Both are opt-in. `off` remains the default; existing `shadow` and `primary`
semantics are unchanged. Restart the Gateway to apply configuration.

`MISSIONOS_JEV_CASCADE_FAST_PATH` defaults to `disabled`. With this default,
Jev's bounded classification alone never skips the reasoner. The only supported
experimental profile, `fixture_verified_detour_v1`, requires all of:

- explicit operator selection of that profile;
- `execution_scope=fixture`;
- Recovery's proposed action and source-verified feasibility both identify
  `avoid_obstacle`;
- telemetry explicitly reports neither stale data nor dropout;
- no custom mission response mapping;
- Jev returns `bounded` for review and routing, and selects `hold` or `replan`;
- no source-declared missing observations, operator decision, or extra reasoning.

This is a fixture evaluation scope, not a validated live operating envelope.
No simulator or hardware fast path is enabled. A future deployment profile needs
separate outcome evidence and explicit review; caller text cannot define a new
profile. The diagnostic HTTP route cannot create task approval candidates.

## Routing contract

Cascade modes add a third independent Jev question, `assessment_route`, alongside
`response` and `review`. Choice, distribution and confidence fields are validated;
confidence is recorded but never used as a routing threshold.

| Signal | Code behavior |
| --- | --- |
| Source declares required observations missing | Return `operator_escalation` with `need_observation`; do not call the reasoner |
| Source declares an operator decision required | Return `operator_escalation` with `human_review`; do not call the reasoner |
| Jev says `need_observation` or `human_review` | Same respective stop paths |
| Jev response is `operator_escalation` | Human review; do not replace the escalation |
| Jev says `bounded` and explicit fixture profile matches | Return Jev's response as a proposal |
| Jev says `deep_reasoning`, source requires reasoning, or profile does not match | Call configured reasoner with the original prompt |
| Jev fails or its routing answer is invalid | Human review; no silent provider fallback |
| Reasoner fails | Human review; no fallback to the earlier Jev answer |

Source declarations are read from `MissionSituation.uncertainty.mission_context`:
`required_observations_missing` (a nonempty list of missing observations),
`operator_decision_required=true`, and `requires_additional_reasoning=true`.
The Gateway builds this context from `mission_context.uncertainty`. Missing
observations and human decisions take precedence over extra reasoning. These
fields can restrict routing; they do not grant permission or expand a profile.

The cascade does not invent missing observations. `need_observation` is an
explicit next-step request, not an automatic sensor or tool invocation. Jev's
classification is fallible and does not prove evidence completeness. The
reasoner receives the original evidence without Jev's answer, avoiding an
additional anchoring input. Its output passes the existing Assurance validator.

## Receipts and authority

`model_invocation_evidence.jev_cascade` records the policy version, selected
profile, route, reason, provider outputs and invocation evidence, and whether the
reasoner was invoked. Provider failures record error types without credentials.
Stops are explicitly adapter templates, not model-generated rationale.

In shadow mode, `jev_cascade_shadow` contains the candidate output and receipt,
`used_for_decision=false`, label agreement and whether it reused the incumbent
call. A candidate stop cannot alter the incumbent output. Incumbent failure is
not replaced by the Jev result. A shadow comparison may wait for both models.

Prediction admission still precedes Assurance. A rejected prediction stops the
workflow before either judge. Human approval or an approved bounded policy,
dispatch-time Rules, Executor, and Verifier remain separate. Neither model
selection nor confidence creates execution authority or observed completion.

## Runtime verification

With Core, CLI and Gateway on `PYTHONPATH`:

```sh
python -m pytest tests/contract/test_jev_assurance.py tests/contract/test_jev_cascade.py tests/contract/test_agent_graph_launcher.py -q
python scripts/smoke_jev_modes_gateway.py
```

The smoke runs real loopback HTTP and the ADK incident workflow. Fixture model
responses cover the three original modes, all four routing outcomes in cascade
and cascade shadow, and the default disabled fast path. It checks exact provider
call counts, unchanged shadow output, and absence of approval/dispatch/executor
activity. It does not establish hosted-model quality or mission outcome value.
