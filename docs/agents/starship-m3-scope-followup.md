# M3: separate live scope-amendment coverage

This follows the frozen four-flight campaign in
[the M3 completion contract](starship-m3-completion.md). It does not replace its
scope-amendment row or change that campaign's unproven result.

## Fixed input and authority

`sixdof_m3_inflight_op_scope_01` is a separately registered, targeted scenario.
It retains the initial publication and nominal-area withdrawal timing of
`op_trial_02`, but contains no reopening estimate or reinstatement publication.
Only the currently delivered notice reaches the model. Neither the scenario ID,
future notice schedule nor a required model action is included in the notice.
This is a designed coverage condition, not a held-out sample or incident rate.

The preflight approval binds the canonical authority, sources, notice manifest,
initial nominal-only return scope, one candidate-bound scope request, deadlines,
resource bounds and fallback. The new scenario grants no implicit authority to
select the next orbit. DeepSeek's call-count cap remains `null`; decision and
mission deadlines still apply. No model prompt or physical guidance is changed.

A current rollout must independently admit the next-orbit candidate. The notice
alone does not prove that a usable return exists. The model may request expansion,
continue observing, or finish without a viable authorized return. The test driver
must not force a request or retry until one appears.

## One-attempt protocol

Before any model call, save the exact canonical authority, all M3/return source
hashes, notice manifest, qualified numerical backend and source commit, together
with a protocol digest outside the flight results. Require at least 5 GiB of free
space. Use a fresh loopback Gateway and output directory. Permit one flight only,
with the existing 2,400-second flight and 2,600-second driver wall bounds; retain
output and failure records. No outcome-driven parameter changes or retries.

```sh
python scripts/start_starship_gateway.py \
  --state-dir "$TRIAL/gateway" --port "$PORT" \
  --project "$SECRET_PROJECT" --fixture-planner --enable-live-mission-director \
  --inflight-operator-token-file "$TOKEN_FILE" \
  --inflight-operator-id m3-scripted-operator \
  --inflight-operator-source scripted_test_driver
python scripts/smoke_starship_m3_operator.py \
  --port "$PORT" --scenario sixdof_m3_inflight_op_scope_01 \
  --output-dir "$TRIAL/operator" --operator-token-file "$TOKEN_FILE" --choice approve
```

The host alone holds credentials. The fixture planner selects the requested
registered scenario; Jev and conditional DeepSeek perform live flight decisions.
The numerical executor runs separately without provider credentials.

The authenticated scripted test operator posts approval **only if the AI requests
it**. It binds the request, observed-state and candidate hashes, then verifies that
replaying the approval returns HTTP 409. This demonstrates the real HTTP/FIFO/
signed-ledger path, not a real person's review, SSO identity or physical execution.

## Report independently

Record separately: model request, authenticated HTTP decision, signed receipt,
one-time scope consumption, subsequent reservation observation, independent
record verification and final Ship outcome. Missing requests are unexercised
coverage; unresolved flights stay unresolved. A valid numerical reference ID
proves a match to the decision-time input field, not the truth of free prose.

Do not count this as a booster perturbation test. The prior four flights contain
four fresh integrated executions but one unique nominal booster trajectory.
Retain the original campaign and this follow-up as separate evidence with their
own source identities. Preserve private raw results; publish only reviewed small
summaries. Stop only this trial's processes after completion and retain unique
flight evidence. Archive/restore verification is required before later deletion.

## Recorded outcome: 2026-10-12

The one flight on source `4f881d6d` ended **FINAL_INTERRUPTED**, not verified
flight or M3 completion. The [follow-up report](../assets/starship-m3-scope-20261012/report.html)
keeps it separate from the original four-role campaign. Protocol SHA-256:
`89fde32a5a0f2dc6742b1ae99c568a670d008ac46aedf759eed60eac1f9a6c30`.

No scope request, HTTP approval or scope consumption occurred. The Gateway
stopped after 585.29 wall seconds at 1,207,678,250 output bytes, above the frozen
1,200,000,000-byte limit. Host free space was 18,631,368,704 bytes: this was the
trial's output bound, not a full disk or a DeepSeek call limit. The failed receipt
is signed but does not authenticate the partial study's bytes as a verified
flight. Read-only production checks of that preserved study passed record
consistency and failed the case (`return_unresolved_or_contact_area_failed`,
`orbital_comparison_case_incomplete`). They do not rehabilitate the Gateway
failure. No retry or limit increase was performed.

The saved requests reveal a control-interface gap, independently of the model's
first choice. At T+1525.45 s the next-orbit candidate had no numeric rejections;
Jev selected `wait_for_update`. The next request was T+2725.95 s, after the scope
request deadline at T+2200.70 s. All nine return requests omitted that deadline
and remaining approval time. Requests after expiry still listed
`select_next_orbit` with `amendment_status: idle`, although the executor would
reject a late scope request. The criterion does explain that selection requests
approval, so the semantics are not wholly absent. Missing deadline information
is demonstrated; its causal effect on the first wait decision is not.

The next bounded implementation should expose the scope-request deadline and
remaining time, wake before that deadline, and remove expired request capability
from the model-facing choices while preserving executor enforcement. Verify
these boundaries with fixtures before another separately approved/frozen flight.
The output chain also repeats nested traces in outer and inner study files;
measure and reduce that duplication with hash-bound artifact references before
revising a future trial budget. Do not silently raise the bound or erase evidence.

Before multi-flight operations, the nominal booster result still needs current-
source perturbation checks, and contact-location numerical uncertainty needs an
explicit margin/convergence assessment. This follow-up establishes neither.

## Verification for this change

- Related M2/M3, operational-trial and replanning tests: 721 passed. Two initial
  local socket failures came from sandbox restrictions; the complete run passed
  with loopback access. Two dependency deprecation warnings remain.
- `python scripts/smoke_starship_inflight_gateway.py --output-dir "$SMOKE"`:
  14 real-loopback HTTP/ledger checks passed, no models or physics.
- The live command above exercised plan/approval, provider host, credential-free
  worker and operator polling, but the final Gateway result failed at its frozen
  output bound. It is not a passing E2E scope-amendment demonstration.
- Saved-record audit: frozen source/authority/protocol and signed failed receipt
  matched; private ledger contained zero requests, receipts and consumptions.
