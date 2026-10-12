# Bounded orbital planning window

This opt-in continuation follows the completed four-call short-deadline trial.
That trial remains 0/4 valid; its records and limits are unchanged. The purpose
is to support mission planning when time is available, not to repair an answer
after its original deadline or rank AI against scenario-encoded fallback Rules.

## Fixed experiment before provider calls

- Input: the byte-verified saved comparison input from the prior development
  flight. This is known-input protocol replay, not held-out mission evaluation.
- Model: DeepSeek Flash, thinking enabled, effort low, 16,384 combined output
  tokens. There is no independently reserved answer-token budget.
- Transport: 120 seconds, one send, no retry, no redirect, 16 KiB request and
  64 KiB response caps. Decision acceptance deadline: 150 seconds.
- Exactly two reserved protocol attempts, at most 32,768 completion tokens.
  Missing usage after a timeout remains unknown. Never overwrite a failed run.
- Two valid, timely answers with independently resolved reference IDs permit
  at most one subsequent live development flight after runtime checks pass.
  Otherwise stop this campaign; do not try new budgets until one passes.
- A protocol result is not flight evidence or a reliability estimate. Reference
  resolution verifies the cited input identities, not the prose or forecast truth.

The [DeepSeek API contract](https://api-docs.deepseek.com/api/create-chat-completion/)
allows an explicit output cap and reasoning effort. These engineering limits
are our own experimental constraints, not a claim about provider latency.

## Approved flight boundary

Only `sixdof_m1_orbital_planning` opts into the new window. Its frozen approval
includes the window, one DeepSeek call maximum, output cap, and reference-ID
protocol. Existing scenarios retain their 35-second contracts and wire format.
The development scenario reuses the comparison-update notice sequence; it is
not a new unseen scenario.

The credential-free executor independently recomputes the available interval
from the decision-time candidates and current notice. A prepared, uncertain
comparison with at least two admissible candidates may use 150 seconds only
when the earliest currently admissible return and notice expiry leave that
duration plus 60 seconds and the bounded coast step. Otherwise it uses the
short decision path. Comparison calculations reserve this planning interval
in advance. A late decision cannot consume the existing return booking.

Time and observations continue while the model runs. A new event invalidates
the old decision; request-bound cancellation releases the synchronous broker
without adopting a late answer. Cancellation does not prove the provider stopped
billing. Already-approved fallback actions still pass independent admission.

Live acceptance additionally requires an actual valid DeepSeek response,
request-bound ID citations, fresh adoption, a dispatch-linked selected return,
and later 6DOF observations. A successful fallback alone cannot satisfy the
new DeepSeek objective. Normal mission success, contact criteria, safety-net
evidence, and this protocol objective remain separate facts.

No guidance or physical-model change is included. The existing synthetic areas,
waiting-resource assumptions, navigation perturbation coverage and integration
step sensitivity limits remain. Real SpaceX operations are not reproduced by
these development authorities.

## Results

The preregistered two-call replay ran once on `3c5f5749`. Both responses were
valid: 21.655 s / 4,474 completion tokens / 12 resolved references, and 39.947 s /
8,964 completion tokens / 10 resolved references. Observed completion usage was
13,438 tokens with no missing usage. There were zero retries and no simulator
execution in this protocol campaign. The second answer exceeded the old
35-second actor deadline; neither answer is retroactively admitted to that old
contract. This is two development observations, not a success-rate estimate.

The approved new bundle SHA256 is
`750566b4e921e604b65e61f935f9af5d772e928ffab75021856e300aaf89a2bf`.
Its complete input retains the earlier verified source input; the wire payload
and budget are new. The old four-call 0/4 result remains unchanged.

Runtime verification before the calls: 717 relevant contract tests passed,
including real loopback HTTP through the production provider transport and
orbital model decoder, cancellation, deadline rejection, approval binding,
legacy-format hashes and independent reference resolution. Ruff and diff
checks passed. The return qualification still reports ready; no physics or
guidance source was changed and the 39 prior flights were not rerun.

The separately reserved **one live Gateway flight passed** on the same frozen
source `3c5f5749`. The flight planner was a fixture; Jev and DeepSeek were live.
The HTTP approval/run flow launched a credential-free 6DOF worker. The verifier
accepted both the records and the case (`contact_and_recovery_area_met`).

| Stage | Observed result |
| --- | --- |
| Initial plan | Jev booked the nominal return. |
| Updated comparison, T+1,627.45 s | DeepSeek compared both candidates and selected `next_orbit`; its 38.863 s response cited 14 valid reference IDs. |
| Continued monitoring | Jev retained that booking through subsequent observations and the final deadline check. |
| Actual dispatch | Revision 2 dispatched `next_orbit` at T+7,731.45 s. |
| Later contact | 26 payloads released; contact at T+10,378.63 s, 4.613 m/s, 2.396 degrees tilt, 62.434 t fuel; 37.9 km from the west area's center. |

DeepSeek selected an earlier, narrower **estimated** service-completion interval
at the cost of waiting energy and propellant. The independent audit resolved all
14 cited identities to the decision-time input. It does not establish the truth
of those forecasts, all free-text claims, or the model's internal reasoning.
Actual recovery service was not simulated. Low-speed contact and the synthetic
area criterion do not establish post-contact survival or physical landing.

This flight used Jev 7 times (7 valid), DeepSeek once (valid), zero inflight human
commands and zero fallback decisions. Human preflight approval was exercised
through the test-operator interface, not authenticated operator identity. Across
the six explicitly listed saved flight studies, Jev is 14/15 valid and DeepSeek
2/3 valid. Protocol-replay calls are excluded; earlier failures remain recorded.
Neither this one development flight nor those small counts are a reliability
estimate. Evaluation concerns performed control duties, not superiority over
scenario-encoded Rules.

Portable reviewed records: [two-call protocol](../assets/starship-comparison/orbital-protocol.json)
and [live flight with cumulative receipts](../assets/starship-comparison/orbital-planning-live.json).
They identify execution source and artifact hashes; full study and credential-free
worker snapshots remain in the local evidence directory.

## Review follow-up and provenance

Claude Opus 5.5, effort high, reviewed this scope in the operator-designated
session. It found that Jev calls in the opt-in window also needed request-bound
cancellation. That small fix was applied **after** the flight. A real loopback
HTTP test holds the first Jev response, cancels it, and verifies that the next
request completes without waiting for the first; Jev's 30-second transport limit
and authority are unchanged. Claude rereviewed the patch with no findings.
Both reviews were read-only; they were not independent simulation reruns.
The final relevant contract suite passed 734 tests, including the real HTTP
cancellation boundary; Ruff, diff, JSON, local-link and portable-asset checks passed.

The recorded flight remains evidence for `3c5f5749`, not for the later source.
The cancellation follow-up has HTTP runtime evidence, with no additional hosted
calls or flight. Physics, guidance and the qualification certificate are unchanged.

## E2E / Runtime Verification

The fixed protocol campaign used the saved original input and a new exclusive
output directory:

```bash
python scripts/starship_orbital_protocol.py prepare \
  --original-probe output/starship-protocol-probe-20261009/offline-final/probe.json \
  --approved-original-sha256 d4931c4c975c83cd83e4ecd1ed51ec62810fcb24e54936178833151fa292b452 \
  --output-dir output/starship-orbital-protocol-20261010/prepared
python scripts/starship_orbital_protocol.py run \
  --bundle output/starship-orbital-protocol-20261010/prepared/bundle.json \
  --approved-bundle-sha256 750566b4e921e604b65e61f935f9af5d772e928ffab75021856e300aaf89a2bf \
  --output-dir output/starship-orbital-protocol-20261010/live-fixed-two \
  --project "$STARSHIP_SECRET_PROJECT" --enable-live-models
```

In separate terminals, the one flight used:

```bash
python scripts/start_starship_gateway.py --fixture-planner \
  --enable-live-mission-director --jev-secret jev-api-key --port 18851 \
  --state-dir output/starship-orbital-protocol-20261010/gateway-state \
  --project "$STARSHIP_SECRET_PROJECT"
python scripts/smoke_starship_chat_gateway.py --port 18851 \
  --scenario sixdof_m1_orbital_planning \
  --output-dir output/starship-orbital-protocol-20261010/live-update
```

Both commands completed successfully; the Gateway was stopped after verification.
The private project locator and raw credentials are omitted. These are recorded
commands, not permission to repeat a spent campaign or overwrite its evidence.
The post-flight cancellation boundary is exercised without provider spend by:

```bash
python -m pytest -q -p no:cacheprovider \
  tests/contract/test_starship_orbital_model.py
```

## Tower registration CI follow-up

Python 3.13 CI reported one tower-registration failure after the orbital update;
3.11 passed. A deterministic fixture reproduces that rejection at monotonic
origins 511.7 and 1023.9: subtracting the signed start from its 75 s deadline
rounds slightly above 75. Registration now compares the absolute deadline with
`issued_time + authorized_duration`, as the actor constructs it. No epsilon or
extra authority is added; advancing either deadline by one representable step
is still rejected. The original CI log did not retain its clock, so this is a
reproduced failure path, not proof excluding every other cause of that CI run.

Before the fix, both fixed-clock positive regressions failed. Afterward,
136 related tests passed, and a real loopback Gateway HTTP status request
confirmed registration of the signed fixture actor at 511.7. Actor/PID/state
were fixtures; this check ran no simulation or model. The live flight above
still belongs to `3c5f5749`, not this follow-up source.

[Runtime summary](../assets/starship-comparison/tower-deadline-runtime.json)
records the tested working-tree source hashes and fixture boundary. The smoke
command was `python output/starship-tower-deadline-20261010/runtime-smoke.py output/starship-tower-deadline-20261010/http`.
Its original script and full local artifacts remain preserved; that existing
output directory must not be reused.

The next proposed milestone is [deployment-to-return handoff](starship-deployment-return-m2.md).
It fixes phase deadlines, observed inventory, reachability and unresolved-outcome
semantics before adding M2 execution. No M2 model call or flight has run here.
