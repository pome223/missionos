# Six-DOF in-flight deployment supervision

This is one bounded MissionOS operating slice in the continuously integrated
six-DOF simulator. It is not a SpaceX recovery procedure, a live spacecraft
interface, or evidence that a model improves mission outcomes.

## Operational basis

SpaceX's [Flight 9 update](https://www.spacex.com/updates/reusability) describes
automatically skipping the deployment objective after excessive attitude error.
It supports keeping measured deployment constraints authoritative; it does not
provide an automatic retry or recovery procedure for this simulator.

ESA's [Juice RIME account](https://www.esa.int/Science_Exploration/Space_Science/Juice/Juice_s_RIME_antenna_breaks_free)
distinguishes actuator firing from actual deployment, using subsequent motion
and deployment confirmation. The [ECSS acceptance-report definition](https://ecss.nl/item/?glossary_id=929)
likewise describes acceptance as forwarding a request to execution. Neither
source turns an acknowledgment into evidence of the requested mechanism effect.

The local synthetic condition therefore accepts the first scheduled payload
release command without creating a separated body. An automatic interlock
inhibits subsequent releases. This is an explicit test fault, not a diagnosis
of a SpaceX mechanism. Source lessons motivate the observation/command/effect
boundary, not vehicle thresholds or the local procedure's safety.

## Approved scope and execution

Use the normal chat catalog entry `sixdof_deployment_supervised`. Its plan binds
the fixed profile and source files, provider mode and maximum calls, and the
procedure `local_deployment_missing_effect_v1`. An explicit `/approve`, followed
by `/run`, grants local simulation and at most one `skip_remaining_deployment`
command. `hold` grants no dispatch. Retry, restart, thrust, attitude, and return
timing changes are outside this scope. Test-operator commands do not establish
authenticated human identity.

The credential-free worker runs the normal six-DOF entrypoint. A Gateway-owned
broker reads one bounded request from a private per-run mailbox and writes one
proposal. Only the Gateway loads provider secrets. Requests contain two distinct
public observations, not the scenario name, fault schedule, hidden recovery
state, final trajectory or future outcome. The simulator never imports provider
code or evaluates an arbitrary command.

Jev chooses a route. `bounded` uses the existing deterministic skip procedure;
`deep_reasoning` permits one bounded DeepSeek hold/skip proposal. `need_observation`
and `human_review` retain the interlock without a command in this slice; their
collect/resolve workflows are not implemented here. Provider failure and invalid
output also hold. An actual Jev call may legitimately avoid DeepSeek. Fixtures
exercise both stages but have no inference invocation evidence.

The flight integrates continuously while a request is pending, paced at one
simulation second per wall second in that interval. Outside that interval it
runs at computational speed. A response is consumed only at a subsequent step;
75 simulation seconds after the request it expires. No state is rewound and no
late response is applied retrospectively. Return time remains unchanged.

Child-side Rules check the request/observation binding, fixed action and route,
one-use budget, observed missing effect, current orbital phase, fuel reserve and
return deadline. Command acceptance changes the sequencer from `inhibited` to
`skipped`. Two later distinct observations across scheduled release intervals
must show the changed sequencer state and unchanged separated-body count.
The independent verifier also links observations to saved six-DOF states and
checks no subsequent release events. It does not infer completed recovery from
the acknowledgment or a model response.

The command's `sequencer_state_before` / `sequencer_state_after` describe the
requested transition. Measured states are the separately timestamped
`observations` and saved sample telemetry. A recorded accepted operation with
later unchanged telemetry can pass record integrity while `observed_effect`
remains false; acceptance is never enough for a positive effect verdict.

The interlock would already prevent further release without an AI proposal.
Observing a skip confirms the bounded sequencer operation, not a counterfactual
payload-saving benefit. The physics, generic guidance, and known contact/return
limitations remain those of the [six-DOF model](../examples/starship-sixdof.md).
`mission_completed=false`, `physical_execution=false`, and no model-value claim
remain mandatory even when output verification passes.

## Running it

Fixture path, no provider requests:

```sh
python scripts/start_starship_gateway.py --fixture-planner --state-dir output/starship-flight-supervision-fixture-state --port 18808
python scripts/smoke_starship_chat_gateway.py --port 18808 --scenario sixdof_deployment_supervised --output-dir output/starship-flight-supervision-fixture
```

A separately enabled live broker can keep planning deterministic, limiting the
provider smoke to one Jev call and at most one conditional DeepSeek call:

```sh
python scripts/start_starship_gateway.py --fixture-planner --enable-live-flight-supervisor --project PROJECT_ID --state-dir output/starship-flight-supervision-live-state --port 18809
python scripts/smoke_starship_chat_gateway.py --port 18809 --scenario sixdof_deployment_supervised --output-dir output/starship-flight-supervision-live
```

`PROJECT_ID` is a replacement for the operator's Secret Manager project. Keys
are read into the host environment, never command arguments, artifacts or
simulator environments. No automatic provider retries or model-value cohort
are part of these commands. Stop a live Gateway after the bounded check.

The standalone six-DOF runner without a broker fails closed to hold. A mailbox
and request ID are execution plumbing, not human approval evidence by themselves.
The MissionOS controller checks the signed grant, source binding, process/run
identity and worker receipt; the independent saved-output verifier cannot
authenticate a human or certify simulator physics.
