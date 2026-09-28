# WAM-informed delivery-pad candidate selection

`src/runtime/yokohama_pad_prediction.py` connects admitted prediction evidence to
the shared `MissionAssuranceAgent`. It accepts an injected judge and returns a
source-bound candidate for review. It does not instantiate a model provider,
approve an action, upload a mission or call the pad-flight executor. There is
currently **no qualified native dynamic-pad forecast provider** for this adapter.

The candidates are independent: fixed zero-displacement `hold`, and an immutable
1–3 m level `vla` step bound to the native response hash. WAM predicts each
candidate's consequences; the mission judge selects a candidate. VLA need not
emit zero to allow a wait. Requiring VLA itself to pick wait was the previous
standalone diagnostic, not the admission condition for this new architecture.
The existing diagnostic results and native model source are unchanged.

The adapter reuses `receive_prediction_evidence` for request, option, model,
mission, policy, environment, observation, state revision and freshness binding.
Its additional pad contract requires:

- City `pad_wait` phase and fixture/simulation scope only.
- Owner-supplied model profile with semantic-validation and simulator-time
  calibration references, independently matched to the prediction binding.
  These references identify externally verified evidence; nonempty strings do
  not prove accuracy. Fixture profiles cannot enter simulation scope.
- Semantic forecasts for **both** options: conflict throughout the interval and
  pad state at its end. An image consistency score or endpoint picture alone is
  not interval occupancy or collision evidence. Unknown pad state cannot support
  advancing; it is not silently treated as clear.
- Forecast horizon covering observation → response → whole candidate duration.
  Model time index is not simulator seconds. The owner supplies candidate
  duration from a separately checked execution contract. No re-dating forecasts
  when inference ends, reusing expired forecasts, or stretching their horizon.
- Fresh current observation, available bounded hold and remaining wait budget.
  Missing these produces no selected action, never indefinite hover.

The judge sees the admitted predictions and both immutable options. Rejected
evidence never reaches the judge. The returned candidate ID must match its
response kind (`hold` / `continue`) and may not modify the delta. A current busy
pad/corridor or a predicted interval conflict blocks a step even if the judge
asks to continue. Prediction admission, proposal selection and execution remain
different facts. An eventual executor must revalidate current occupancy,
reservation, geometry, battery, hold limits, freshness and operator scope using
the existing independent Rules. No new aircraft response is dispatched here.

`scripts/check_yokohama_pad_prediction.py` supplies explicit synthetic semantic
forecasts through `PredictionRegistry`, and a deterministic judge that reads the
actual Mission Assurance prompt. No expected answer, case ID or actor schedule
is used by that judge. The first two scenarios have identical current facts and
VLA requests: changing only the forecast changes `hold` to `vla`. This establishes
data-path sensitivity, not learned-model accuracy or mission benefit. The other
cases cover current occupancy, expired horizon, missing calibration, changed
revision, lost hold and sea-phase rejection.

With `--native-probe`, the script independently reopens the published native
input/output hashes. Those old images remain unqualified: no dynamic semantic
reader, uncalibrated physical horizon, and the unchanged reference coverage gate
fails. They are not converted to invented occupancy labels or fed through the
synthetic predictor. No extra inference, AP flight or post-training occurs.

Next native work requires time-aligned recordings of unloading, departure,
stalled departure and reoccupation at the actual decision viewpoint, an
independently evaluated semantic reader, and a horizon compatible with measured
latency. Use separate development/evaluation sequences before training. The
question is whether WAM evidence can support the mission decision; perfect
images or beating ideal Rules are not requirements.

[CPU connection report](../examples/yokohama-pad-prediction/REPORT-ja.md)
