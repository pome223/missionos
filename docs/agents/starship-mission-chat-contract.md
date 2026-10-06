# Starship MissionOS chat, worker, and artifact contract

This opt-in numerical simulator uses the normal CLI/Gateway plan, approval,
execution, and status boundary. Public defaults are deterministic fixtures;
fixture catalog matching is not inference. Source code for live planner/Jev
modes is separate opt-in and does not authorize a provider call by itself.

## Authority and current catalog

The plan identifies a fixed supported scenario, profile/source hashes, runtime
bounds, and any separately approved bounded procedure. `/approve` binds that
one plan; `/run` consumes its grant once. Approval does not execute. Ambiguous
assent, wrong plan/session, expiry, repeat dispatch, and source changes fail
closed. Test-operator approval does not authenticate a human identity.

The catalog includes coupled launch/fault cases, initialized 30-second actuator
cases, deployment supervision/observation, retained-payload return policies,
initialized terminal arm tests, and conditional launch-connected catch. These
names identify executable experiments, not promises of objective completion.
The tower/preburn scaffold does not activate a return-site driver or add a
qualified capture/divert catalog entry.

The simulator integrates continuously while a deployment judgment is pending.
Rules constrain one allowed operation against current evidence, the original
deadline, phase, fuel, return budget, and one-use scope. The model cannot change
thrust/attitude controls, reset state, waive catch limits, or convert its own
proposal into approval. Later sampled sequencer states determine observed
effect; ACK/acceptance alone does not.

## Process, source, and evidence boundary

The host starts a separate credential-free worker. Providers remain in the
separately enabled host broker, never in the simulator environment. A signed
worker receipt and source/profile checks bind the outcome to the chosen plan.
The Gateway serves only verified session/plan artifacts and checks their
hashes. Provider exceptions/keys must not leak into logs or public artifacts.

Local same-OS-user signing keys provide software integrity under that trust
boundary, not independent human identity or adversarial same-user isolation.
Source-file SHA checks do not authenticate already imported code. Record
verification can pass for impacts, timeout, and failed objectives. Keep
`mission_completed=false`, `physical_execution=false`, and vehicle/model-value
flags distinct from saved-record integrity.

## Public runtime verification

Install the repository and CLI/Core/Gateway packages as in the README. Use
the CPU extra `python -m pip install -e '.[spaceflight]'` and fresh state/output
directories. The short fixture makes no provider request and needs no native
Basilisk dependency:

```sh
python scripts/start_starship_gateway.py --fixture-planner \
  --state-dir output/starship-public-runtime-state --port 18832
```

In another terminal:

```sh
python scripts/smoke_starship_chat_gateway.py --port 18832 \
  --scenario sixdof_gimbal_step --output-dir output/starship-public-http-smoke
```

On the fresh public checkout this route observed
`awaiting_approval -> approved -> running -> verified`, a distinct worker PID,
30 simulated seconds and 80 saved samples. Worker receipt verification and all
four served artifact hashes passed. Missing approval, ambiguous assent, wrong
plan hash, repeat dispatch, and cross-session artifact access were rejected.
No Jev/DeepSeek call occurred; planner mode was a deterministic fixture.
`test_operator=true`, authenticated-human-identity=false, and mission/physical/
vehicle-validation flags remained false. Generated runtime output is local
ignored evidence and is not committed.

The current public-port software checks passed 3,159 tests across 98
Starship/Jev files, with one skip because native Basilisk was not installed
(288.22 s). A separate 72-test existing Go2/Yokohama/ship fixture subset passed.
AST/Ruff checks covered 220 Python files; the 85-module import closure and
existing CLI help also passed. These fixed-fixture checks made no new private
return-campaign, provider, native Basilisk, or hardware invocation. They are
software-boundary results, distinct from the historical videos and from
spacecraft engineering validation. Public CI status is reported separately
on the pull request; local checks do not imply CI completion.

This smoke covers the initialized actuator and production HTTP/child/artifact
boundary, not full launch, catch, native Basilisk, or later-effect supervision.
Stop the owned Gateway after checking it. If relevant production source changes,
restart it and repeat the affected boundary with a fresh plan and output.

To exercise the longer deployment boundary separately, explicitly budget the
full numerical run, restart a fixture Gateway, and use:

```sh
python scripts/smoke_starship_chat_gateway.py --port 18832 \
  --scenario sixdof_deployment_supervised \
  --output-dir output/starship-public-supervision-new
```

Expected evidence is two fresh observations, a current Rules decision, at most
one skip operation, and two later distinct measured sequencer states. The
[supervision contract](starship-flight-supervision.md) defines exact scope.
That integration demonstrates a bounded sequencer operation; the interlock
already prevented further release, so it supplies no counterfactual model
benefit. Historical live routing used one Jev bounded route and no DeepSeek
call. Do not describe the public deterministic fixture as that live invocation.

The [human showcase](../examples/starship-mission-showcase.md) links only reviewed
derived media and public commands. Private task databases, approval keys,
provider exchanges, full raw campaign traces, and local-only absolute paths
must never be imported into the public repository.
