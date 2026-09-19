# Prediction evidence admission into Mission Assurance

This slice follows the common Prediction interface. It ends at a bound
`MissionSituation` containing a prediction admission receipt, and the existing
Assurance prompt projection. Intake itself invokes neither the LLM judge nor an Executor.
It is not wired into the Gateway or a live robot observation lifecycle.
The separately enabled [stacking mission E2E](stacking-mission-e2e.md) composes
this intake with an actual LLM, bounded policy and simulator execution.

```text
PredictionRegistry forecast + original PredictionRequest
  -> trusted observation owner captures evidence context
  -> receive_prediction_evidence(current MissionSituation, evidence)
  -> adopted/rejected receipt in MissionSituation.uncertainty
  -> existing Assurance prompt projection
```

Admission means eligible as model-inferred judgment material. It does not mean
that an LLM used the evidence, that the prediction is correct, or that a mission
response was selected. Risk is neither feasibility, approval, nor execution
permission. The existing LLM judgment and downstream human/Rules boundaries are
unchanged. No threshold-based response selector or automatic fallback is added.

## Contracts and trust

`capture_prediction_evidence` snapshots the original request, forecast and context
as `missionos_prediction_evidence.v1`. Its caller must be the trusted observation
owner at prediction time, before subsequent world-changing actions.

The current situation must independently provide:

- `mission_contract.prediction_contract`, matching the registered mission binding;
- `constraints.prediction_context.binding`: the complete expected model ID/digest,
  policy digest, environment contract, input schema, and mission contract;
- `prediction_context.execution_id`, `state_revision`, `observation_id` and
  `source_ref`: nonempty identifiers matching the captured context.

The observation owner must change the revision after any world-changing action
and update observation identity on new observations. Revision equality invalidates
an older forecast even if its timestamp is recent. The intake does not monitor
an Executor or generate authoritative revisions itself. Context must not be
copied from an untrusted incoming forecast to manufacture a match. These local
JSON inputs and their hashes are not authenticated provenance or physical truth.

Callers supply `now` and a positive finite `max_age_seconds`; the CLI reads the
current clock and requires an explicit maximum age. Intake checks context,
mission, original request digest, forecast identity, model-inferred source,
false authority flags, availability, age, option coverage, horizons, finite
risk in [0, 1], and structured future state. Malformed evidence is rejected.
Invalid caller freshness policy raises an error. A digest binds identity, not
prediction accuracy, calibration, or arbitrary input-schema semantics.

The return value is a new `MissionSituation` and a
`missionos_assurance_prediction_admission.v1` receipt. The receipt binds the
source situation digest and evidence digest, admission status/reason, evaluation
time and age policy. Authority, feasibility and LLM invocation flags remain
false. An unreadable/nonfinite envelope has no evidence digest.

Adopted forecasts retain context, request digest, horizon, risk, and opaque
future state in `uncertainty.prediction_evidence`. They never enter observed
facts. Unavailable, stale, invalidated, or mismatched evidence produces a rejected
receipt with no forecasts. Every call replaces previous prediction evidence,
including on rejection, so prior accepted predictions do not survive a failed
refresh. The situation input digest changes to bind the resulting evidence.
Admission must be rerun against current context before any later judgment; this
receipt is not a durable permission and does not renew freshness.

## Runtime verification

From the source checkout with the existing Core/CLI and Pydantic dependencies:

```bash
PYTHONPATH=packages/missionos-core/src:packages/missionos-cli/src:. \
  python scripts/smoke_assurance_prediction_evidence.py
```

The public smoke constructs a synthetic battery forecast through the actual Core
registry, writes fresh fixture inputs, invokes the production CLI in a subprocess
for each case, reads its persisted receipt, and projects the returned situation
through `build_mission_assurance_prompt`. Temporary files are removed on exit.

| Case | Result |
| --- | --- |
| Available, current, matching forecast | Adopted; forecast reaches prompt uncertainty |
| Model unavailable | Rejected; no forecast in prompt |
| Old observation | Rejected; no forecast in prompt |
| Different policy binding | Rejected; no forecast in prompt |
| State revision changed after an action | Rejected despite recent timestamp |
| Current context absent | Rejected |

All six CLI invocations exit zero because rejection is a recorded intake result,
not a failed process. Reusing an output path fails rather than overwriting an
existing receipt. No learned weights, paid services, LLM calls, simulator or
hardware execution are involved. The battery fixture demonstrates a non-stacking
contract only; it is not evidence of another learned mission model's capability.

The CLI can also receive operator-supplied trusted inputs:

```bash
python -m missionos_cli prediction admit-evidence \
  --situation "$SITUATION_JSON" --evidence "$PREDICTION_EVIDENCE_JSON" \
  --max-age-seconds 2 --output "$NEW_RECEIPT_JSON"
```

The output contains both the updated situation and receipt. It does not dispatch
or call the Agent. Automatic Gateway/Agent ingestion, authenticated provenance,
revision lifecycle integration and evidence-dependent LLM behavior require a
separate change and runtime verification. The prior stacking six-case simulator
record is unchanged and is not claimed as verification of this intake slice.
