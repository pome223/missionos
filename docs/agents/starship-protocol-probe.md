# Bounded response-protocol check after M1

M1's recorded mission-control work remains valid. The subsequent live numerical
citation attempt exhausted its 4,096-token completion budget without an accepted
answer. This task checks a response interface, not AI superiority over a
scenario-encoded policy or the physics of another flight.

## Scope and exit

The preparation entrypoint is offline. A separate, explicit opt-in runner now
executes the approved four-call campaign without simulator launch or dispatch.
Existing flight contracts, request builders and numeric-evidence v1 remain
unchanged. The hosted campaign and its stop decision are recorded below.

The proposed bounded campaign is fixed before any new inference:

| Order | Protocol | Effort | Completion cap | Attempts |
| --- | --- | --- | ---: | ---: |
| 1–2 | decision-specific reference IDs | low | 4,096 | 2 |
| 3–4 | decision-specific reference IDs | high | 8,192 | 2 |

There are at most four calls and 24,576 generated tokens, including thinking.
Each attempt has a 30 s transport timeout, 35 s decision deadline, 16 KiB request
cap and 64 KiB response cap, with no retries. Preparation itself is **not an
execution approval**. The user subsequently authorized this fixed campaign.
The runner binds that authorization to the saved bundle hash and durably reserves
all four slots before sending. It measures elapsed time, preserves every result,
rejects an existing campaign directory, and stops remaining calls on a reported
output-cap violation. The offline audit alone uses supplied elapsed time; the
runner records actual transport and audit time.

Both scheduled attempts for a setting must produce a valid, timely answer with
valid reference coverage. If both settings pass, prefer the first registered
setting. Either permissible action can pass; the test does not encode the desired
mission decision. Two successes are only readiness for one bounded flight, not a
statistical reliability estimate. Preserve failures and do not add attempts or
change the prompt after seeing results.

- If a setting passes, integrate that exact protocol under a new flight approval
  and run one bounded live flight. A protocol response alone is not flight proof.
- If neither passes, stop tuning this 35 s interface. Restrict the *next proposed*
  DeepSeek application to decisions with sufficient time and an explicitly
  checked deadline, booking and fallback. Do not silently extend existing waits.

## Exact original replay and a distinct new protocol

`prepare_starship_protocol_probe.py` reconstructs the selected request with the
production builder behind a capture-only transport. It reconstructs earlier call
slots and requires the final provider-body SHA-256 **and** byte length to match
the original receipt. Similar semantic material is not enough. Changes in source
behavior or ambiguous history fail at this boundary. Only the decision-time
request and earlier routing state enter reconstruction; later observations and
the final flight result are excluded.

The ID variants have new payload hashes. They must never be described as exact
replays of the old provider body. This is a previously seen development input,
not held-out evaluation or evidence of general operational reliability.

`catalog_for` binds each ID to the full decision request and its candidate,
field path, value and unit. The model returns IDs, not copied values or a copied
request hash. The independent audit reconstructs the catalog from the original
request. It rejects unknown or stale IDs, duplicates, unobserved fields and
missing references for a compared candidate. Resolved values are explicitly
**host-resolved**; this does not verify numerical assertions written by a model,
its prose, its internal reasoning, forecast truth or the quality of the action.

## E2E / Runtime Verification

The actual CLI was invoked on decision index 1 of the saved failed live flight:

```bash
python scripts/prepare_starship_protocol_probe.py \
  --study output/starship-citations-live-20261009/live-update/study.json \
  --decision-index 1 \
  --output-dir output/starship-protocol-probe-20261009/offline-prepare
```

The selected record produced the same 11,400-byte original provider body with
SHA-256 `35f9b0d8db402b5ff14cc2b9366ff81a79875cbc849d261ef96813eacd6ee840`.
The low/high ID variants were 13,323 / 13,324 bytes, below the unchanged cap.
This checks CLI parsing, the real request builder, capture isolation, wire
identity, protocol construction and exclusive artifact persistence. It does not
check a model's response: hosted calls = 0, flight runs = 0.

The bundle includes decision-time material and complete prepared provider
payloads. Keep it in ignored local output; do not automatically publish it.
The CLI requires a fresh destination and refuses to overwrite earlier evidence.
Its `summary.json` contains hashes, sizes and the proposed limits without raw
model text. Supplied-response tests exercise truncation, malformed JSON, wrong
model, deadline, budget, stale references and both permissible actions.

## Fixed hosted campaign at `ee79ec2b`: stop condition reached

The [sanitized campaign record](../assets/starship-comparison/protocol-campaign.json)
contains four attempts from the unchanged prepared bundle
`d4931c4c975c83cd83e4ecd1ed51ec62810fcb24e54936178833151fa292b452`.
All eight execution-source hashes match this commit. Independent record reading
confirmed the reservation, individual attempt files, summary, input hashes and
the low/low/high/high order. No prompt or limit was changed between calls.

| Attempt | Setting | Elapsed | Result | Completion / reasoning tokens |
| --- | --- | ---: | --- | --- |
| 1 | low / 4,096 | 20.103 s | `output_limit` | 4,096 / 4,096 |
| 2 | low / 4,096 | 20.293 s | `output_limit` | 4,096 / 4,096 |
| 3 | high / 8,192 | 30.010 s | `transport_timeout` | unknown |
| 4 | high / 8,192 | 30.007 s | `transport_timeout` | unknown |

Neither setting met 2/2. Accepted answers and verified ID references were zero.
The observed completion-token total is 8,192 **plus unknown usage for two
timeouts**, not 8,192 total consumption. The maximum reserved completion budget
was 24,576. High-setting timeouts do not establish output-budget exhaustion or
whether an answer would have arrived in the remaining actor time.

The predeclared decision is `stop_short_deadline_tuning`. **This short-deadline campaign performed no retry, integration
into the flight contract, or live flight.** This shows that the
tested response contract did not meet its short-deadline readiness gate on this
known input; it does not show that AI cannot conduct missions, that reference IDs
are inherently unsuitable, or that rewriting numbers caused the earlier failure.
Prior M1 control evidence is not reclassified by this protocol result.

Further DeepSeek work belongs to a planning interval with sufficient time,
explicit candidate/booking deadlines and an independently checked fallback.
The separately approved [orbital planning window](starship-orbital-planning-window.md)
now records that continuation. It does not extend this campaign's limits or
silently change already approved flights.

The real CLI invocation was:

```bash
python scripts/run_starship_protocol_probe.py \
  --probe output/starship-protocol-probe-20261009/offline-final/probe.json \
  --approved-probe-sha256 d4931c4c975c83cd83e4ecd1ed51ec62810fcb24e54936178833151fa292b452 \
  --output-dir output/starship-protocol-probe-20261009/live-fixed-four \
  --project "$STARSHIP_SECRET_PROJECT" --enable-live-models
```

The private project locator was obtained from the existing gcloud configuration
and is omitted here. The CLI retrieved the key from Secret Manager into its own
process; neither key nor raw provider output/reasoning was retained. It exited 0
because the bounded campaign completed, **not** because a setting passed.
The runtime boundary includes the real CLI, durable reservation, fixed-endpoint
HTTP calls, response audit and exclusive record persistence. This is four hosted
protocol calls and zero simulator or vehicle executions. A separate localhost
HTTP-stub test exercises the production transport with four valid fixture
responses and is never counted as model inference.

After this recorded run, the CLI's final display was changed to print the actual
attempt count if a future campaign stops early on a provider cap violation.
That display-only correction was not followed by another hosted run; execution
evidence remains bound to `ee79ec2b`.

## Integration boundary

This public snapshot includes the event-supervision and response-protocol work,
followed by M2 deployment-to-return control and the completed bounded M3 matrix.
See [the M3 completion contract](starship-m3-completion.md) for the current
claim and [the campaign report](../assets/starship-m3-final-20261012/report.html)
for the reviewed results. The protocol trials above retain their own source
identities and outcomes; publication does not turn an earlier failed response
into a successful decision or a new live-model run.

The snapshot is applied to public `main` without importing private development
history. Runtime sources and qualification artifacts retain their reviewed
content. Provider credentials, raw request/response captures, operator keys and
private experiment directories are not part of this publication.
