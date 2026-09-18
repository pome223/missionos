# MissionOS prediction boundary: stacking integration verification

Date: 18 September 2026. Scope: a common prediction contract, the frozen stacking
adapter, and a live simulator executor. This is a separate implementation change
from the [block-stacking research report PR](https://github.com/pome223/missionos/pull/102).

## What runs through MissionOS

```text
missionos prediction serve-stacking
  -> PredictionSession receives current observation/options
  -> Core PredictionRegistry checks model/policy/mission binding
  -> StackingPredictor runs the frozen lightweight WAM
  -> configured stop rule selects continue or bank
  -> explicitly enabled simulator executor applies that option
  -> MissionOS receives measured outcomes and compares matching forecasts
```

The request cannot supply realized future actions or future outcome fields.
The decision is persisted before its selected option executes. Real SmolVLA
inference supplies lowering/release actions; the environment stages acquisition
and uses shared approach/withdrawal controllers. Simulator state, not a generated
image, provides the outcome. The new WAM predicts placement plus hold over 28.4 s;
an immediate 14.2 s placement result remains explicitly incomparable. A labeled
saved-state extra-hold branch supplies the matching forecast observation. All
terminal scores require a 14.2 s hold after the last placement.

This is the MissionOS **Core/CLI simulator integration**, with a deterministic
configured stop policy. It does not invoke the main LLM Agent, Gateway approval
or dispatch route, Mission Assurance, or hardware. Predictions remain
`model_inferred`; they do not create execution authority or completion claims.
The simulator run was explicitly enabled by the operator. The standalone
loopback service is not an authenticated production multi-user API.

## Known-case integration checks

Six previously inspected cases were chosen before execution: a useful warning,
the two missed hazards, and the three safe-ten false stops. These are regression
and integration cases, not new held-out capability evidence. No weights, stop
thresholds, VLA policy, or training data changed.

| Seed | Prior WAM score | MissionOS integration score | Behavior retained |
| --- | ---: | ---: | --- |
| 67002 | 8 | 8 | Banks eight before delayed ninth-placement collapse |
| 67006 | 0 | 0 | Misses ninth-placement hazard; later banking collapses |
| 67010 | 0 | 0 | Misses ninth-placement hazard; later banking collapses |
| 67016 | 9 | 9 | Rejects a safe tenth placement |
| 67020 | 9 | 9 | Rejects a safe tenth placement |
| 67036 | 9 | 9 | Rejects a safe tenth placement |

Preserving both successful and erroneous decisions demonstrates that integration
did not silently replace the model or improve the benchmark through different
rules. It is not a new comparison against fixed stopping and does not establish
better predictive capability.

The adapter was first compared numerically on sixty stored decision states:
risk scores and predicted poses matched the original predictor to tolerance
1e-12. A live run then used fresh VLA/WAM inference and simulated selected actions.
An independent audit checked current input states, choices, predicted poses,
selected control tapes, simulated trajectories, and game scores against the saved
reference cases. This comparison uses known seeds to verify integration fidelity,
not to claim independent generalization.

The final live audit matched **59 decisions**, **16,756 selected motor steps**,
and **689 actual VLA inference chunks**. All six scores, input states, selected
control tapes, and simulated trajectories matched the prior cases (state/trajectory
tolerance 1e-9; risk/forecast-pose tolerance 1e-12). Decisions were recorded before
selected execution produced outcomes.

There were 59 selected-execution receipts. Of these, 53 immediate continuation
receipts were correctly marked incomparable with the new model's longer horizon;
their 53 separate placement-then-hold observations yielded 51 TN and two FN.
The six selected bank observations yielded four TN, one FN, and one TP. A TP for
bank risk still corresponds to an actual collapsed bank, not successful stopping.
These counts concern selected options and their labeled hold branches, not the
full continue/bank classification table of the prior forty-game evaluation.

VLA and MissionOS service processes exited with code zero, and all six simulator
containers exited successfully. No cloud GPU was used. The final live run followed
public-base integration and validation changes; an earlier local smoke was kept
separate and is not counted as additional capability evidence.

## Reproducible public boundary checks

The fixture tests run without private models or simulator artifacts:

```bash
PYTHONPATH=packages/missionos-core/src:packages/missionos-cli/src:. \
  python -m pytest -q tests/contract/test_missionos_core_action_feasibility.py \
  tests/contract/test_mission_prediction.py tests/e2e/test_mission_prediction_http.py

PYTHONPATH=packages/missionos-core/src:packages/missionos-cli/src:. \
  python -m missionos_cli prediction --help
```

After the Core/stacking comparison separation: 29 tests passed; CLI entrypoint exposed `serve-stacking`. Tests include
cross-model/policy/mission/environment mismatches, stale input, input mutation,
unknown future fields, duplicate decisions/outcomes, unsupported outcome horizons,
wrong action bindings, unsupported score/collapse labels, and unavailable-model
fallback. The HTTP smoke exercises the actual service implementation with a
synthetic predictor; synthetic outputs are not presented as learned predictions.
The latest revision additionally checks all four stacking confusion outcomes and
rejects horizon mismatches before classification. Core only binds observation
references and carries no collapse or confusion-matrix semantics.

The six-case live simulator result above was recorded at `ac2db1f`, before this
comparison refactor. It was not rerun for the refactor; the updated production
HTTP decision/observation path was exercised with the synthetic predictor.

The live command and external dependency requirements are documented in the
[contract](mission-prediction-contract.md). The live model is the unchanged
new stacking checkpoint with SHA-256
`fac4cf0ea85e1d7eef3a0af361e842b88fbe60148003530ac00c337b4660730b`;
SmolVLA SHA-256 is
`254ec40e3be4a44f62073be9cea999ed165ca533d26204beb7f1238890fd38b4`.
The local ML runtime used scikit-learn 1.9.1. Loading rejects a mismatched digest
or an incompatible serialized scikit-learn version. These identifiers bind
local audited artifacts; weights and raw experiment records are not published.

## Limits and next boundary

The common interface can host mission-specific predictors, but only this learned
stacking adapter has been exercised here. Exact simulator state and known
physical properties remain required. Unknown material inference, new controllers,
real robots, and another mission have not been validated. The simulator harness
requires external trusted environment modules and a separately running frozen
VLA service; this PR is not a full public replication package.

The next product integration is to let Mission Assurance receive these bound
forecasts as evidence, before connecting the Agent. Human approval, deterministic
constraints, execution, and verification remain separate steps. A full Agent/Gateway
mission loop must have its own runtime verification; it is not implied by this
Core/CLI lab result. New mission models also require their own outcome-based
comparison with simple baselines before adoption.
