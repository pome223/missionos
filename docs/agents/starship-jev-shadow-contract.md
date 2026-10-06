# Starship Jev fault-routing shadow

## Frozen purpose and selection

This is an integration and information-boundary check on the existing synthetic
dispenser. It does not estimate model value, validate Starship hardware, or change
flight physics. The numeric history rule already matches the finite-model optimum
in released payload count across the 60 evaluation cases. A routing label has no
independent ground-truth correctness label in this experiment.

The baseline remains the frozen training-selected history rule. Run the original
870-record study, then rerun its 60 evaluation/history-rule cases on identical
world tapes. Emit only the **first decision following an observed failed retry**
in each case. This public-history selection gives 22 eligible cases and 38 cases
without a trigger. A failed retry at a terminal boundary has no following decision.
Do not substitute initial observations or later points to fill a call quota.

Jev receives all 22 eligible public prefixes, with no request cache and at most
22 attempted sends. There is no automatic retry, resumption, parameter search,
text-note experiment, DeepSeek call, or replacement of an unfavorable response.
Training cases and the remaining decisions are not sent. Evaluating every decision
would require a different experiment: the 60 incumbent runs contain 542 decisions.

## Authority and processes

The new catalog item is `dispenser_jev_shadow`. Planning binds mode, a zero-call
fixture or maximum-22-call live budget, trigger, source hashes and evaluation scope.
`/approve` signs a distinct `local_simulation_and_jev_shadow` grant; `/run` consumes
it once. A previous plain simulation grant cannot authorize provider calls.
Changing environment settings after fixture approval cannot turn it into live mode.

The approved subprocess is an **observer broker**. Only this role may receive
`TYPESAFE_API_KEY`; it does not receive a DeepSeek key. The broker launches another
simulator process using the existing credential-free environment allowlist.
The simulator also rejects credential-bearing environment names itself.

The simulator emits public observations to stdout. Its stdin is `DEVNULL`.
There is no route, acknowledgment, action, or approval channel back from Jev.
A reader thread drains at most 22 observation frames and one completion frame,
with a 16 KiB line limit. The broker rejects duplicate case references and malformed
indices before contacting the provider. All 60 reruns must exactly match every
baseline step and terminal result, including wait 2 s / retry 4 s simulation time.

Provider wall latency and the simulation clock are separate. Processing can lag
behind the simulator or finish after it exits. This is asynchronous observation
shadow, with **no real-time supervision guarantee**.

The signed outer execution receipt identifies `process_role=jev_shadow_observer_broker`.
Its `provider_credentials_present` describes that broker, and can be True in live
mode. `simulator_provider_credentials_present=False` describes the distinct child.
Do not relabel the broker's credential presence as a credential-free process.

## Provider input and routing

The strict router accepts only `PublicHistory` and `PublicBudget`, checking exact
keys, primitive types, observation/retry ordering, current time, attempts and released
counts. The payload adds only whitelisted fields of the public finite law.
It excludes scenario name, case reference, group, seed, realized recovery time,
future readings, oracle values, terminal results and incumbent action.
Not knowing the realized recovery time and observing a noisy sensor are normal
features of the model; they are not automatically missing required observations.

| Proposed route | Role that would receive the case |
| --- | --- |
| `bounded` | Existing history rule |
| `need_observation` | Observation collector |
| `human_review` | Human review |
| `deep_reasoning` | DeepSeek proposal role |

No target is invoked by this shadow. The existing Jev cascade cannot be reused
unmodified: its `bounded` path outside the legacy fixture fast path invokes a
reasoner. The new routing-only adapter uses the same four routing semantics while
preventing that invocation. Jev never chooses retry/wait/abort here.

The endpoint is fixed HTTPS; proxy inheritance, redirects and retries are disabled.
Limits: 16 KiB request, 64 KiB response, 15 s response acceptance deadline and socket
timeout. A late response is discarded; an already-issued network request cannot be
recalled. Its reserved slot remains consumed and inference is unconfirmed.
Only a valid typed response confirms model inference. Raw provider bodies, exception
messages and keys are not saved. Confidence is uncalibrated and grants no authority.

## Records and independent verification

`study.json` contains the original baseline, the 60 reruns and a shadow sidecar.
The baseline's `llm_invoked=False` refers only to its unchanged simulation; live
Jev calls are recorded separately in the sidecar. Do not promote that field to a
claim about the whole broker run.

The verifier checks the original 870 records, exact equality of all 60 reruns,
independently derives eligible frames from completed baseline retry receipts,
validates input/request hashes, routing labels, call slots, failures, mode and
no-authority fields, and rehashes the bound repository sources. It does not call
Jev or rerun the simulator. A consistent stored artifact alone cannot independently
authenticate a provider call or OS process; outer runtime receipts remain separate.

The summary reports 60 evaluated / 22 triggered / 38 untriggered, attempted calls,
observed valid responses, failure statuses and route distribution. These are
coverage and diagnostic measurements, not routing accuracy or mission improvement.
Interrupted runs retain sanitized attempt intents and receipts in their new output
directory. Reusing the directory or consumed approval is refused.
