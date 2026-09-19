# Governed stacking mission demonstration

This opt-in simulator adapter connects the Prediction contract and Assurance
intake to actual mission-level LLM judgment, a bounded approved policy, dispatch,
simulator motor execution, measured outcome verification, and the next step.
It is a MissionOS CLI composition, not a Gateway deployment or hardware trial.

## Actual responsibility chain

1. The staged simulator supplies exact object/robot state and the next registered
   placement macro, bound to the frozen SmolVLA policy and environment.
2. Core invokes the frozen lightweight stacking WAM. Its future poses and risks
   are model-inferred evidence, not feasibility or permission.
3. Assurance intake binds that evidence to the current mission observation.
4. The actual `MissionAssuranceAgent` calls the configured LLM (DeepSeek for the final validation). It proposes
   `continue`, `hold`, or `operator_escalation`. No deterministic fallback
   substitutes for a missing or invalid LLM judgment.
5. The stacking compiler maps `continue` to the registered placement macro and
   `hold` to `bank`. Here bank is the existing noncontact zero-motion 14.2-second
   hold; it does not approach or release the next block. Escalation dispatches
   nothing. The Agent cannot supply new motor parameters.
6. Stacking Rules revalidate the frozen decision, current observation and state,
   prediction freshness, registered macro, mission contract, approved policy,
   expiry, and remaining action budget. A durable `PolicyStore` reservation
   precedes the dispatch ticket. A duplicate ticket request is rejected.
7. The explicitly enabled simulator executor accepts that ticket, performs the
   selected macro, and records the actual action array and VLA RPC identifiers.
8. The measurement receiver requires the matching dispatch ticket and checks
   horizon, measured per-block drops, collapse label, count and score consistency.
   Only after that receipt can the next decision begin. `mission_complete` is a
   terminal-game marker, including a zero-point collapse, not a generic mission-
   success or physical-completion claim.

The WAM is conditioned on a VLA/controller **macro**, not an already generated
future motor tape. Actual closed-loop SmolVLA chunks are produced during the
placement. This preserves the model's training contract; it is not an arbitrary
motor-action-conditioned world model.

The earlier lab stop rule is retained only as `baseline_reference_option` for
audit. It is neither dispatched nor supplied as a recommendation to the LLM.
Risk and the classifier's reference threshold remain evidence. Scores need not
match earlier deterministic-rule games because the actual LLM now chooses.

## Risk semantics and provisional score comparison

The frozen WAM's risk is the positive-class output of an ExtraTrees classifier
trained with balanced class weights. It is **not a calibrated physical collapse
probability**. The positive event is a score-bearing block dropping more than
0.03 m within the option horizon. The separate future-pose/drop regression head
is fallible evidence about the same rollout, not another independent probability.

The stacking mission supplies `constraints.stacking_score_comparison` before
Assurance admission, so the resulting situation digest also binds the arithmetic.
For current count `n`, it explicitly substitutes raw risk for failure probability
**only as an illustrative proxy**:

- Bank proxy points: `n * (1 - bank_risk)` over the 14.2-second hold.
- Continue-then-bank proxy points: `(n + 1) * (1 - continue_risk)` over placement
  plus terminal hold, 28.4 seconds. Bank risk is not applied a second time.

The prompt requires both proxy values in the rationale, distinguishes them from
true expected points, and specifies the game's linear point objective. Collapse
to zero is already represented in this arithmetic; an unspecified additional
loss-aversion penalty must not be introduced. A contrary choice must cite
concrete additional evidence. This changes the decision framing and may influence
the LLM; it is not calibration, an optimal ten-step planner, or a neutral test of
an unchanged prompt. The helper returns no recommended action and creates no
authority. The actual LLM still proposes; approval and Rules remain separate.

## Approval and Rules

`--approve-simulator-mission --operator <authorization-reference>` records the
operator's bounded preapproval in the existing `PolicyStore`. It is distinct
from a fresh human click for every action, and the artifacts explicitly record
`individual_action_human_approval=false`. The authorizing user request must
actually cover the selected simulator missions; the LLM cannot approve policy.

The approval covers only named seeds, the frozen model/VLA/environment binding,
the registered continue/bank macros, at most ten decisions per game, simulator
scope, and a two-hour expiry. Each selected operation consumes a durable budget
reservation. The new stacking-specific Rules adapter uses the existing policy
contract/storage; it does not fabricate a Recovery Agent incident graph or claim
to have exercised the existing recovery-continuation graph.

Each placement executes 284 motor steps. A selected bank executes 284 hold steps.
At ten completed placements, the last continuation includes an additional 284
terminal hold steps in its authorized budget. No extra counterfactual simulator
branches run. Immediate continuation observations are incomparable to the WAM's
568-step future; the terminal placement-plus-hold measurement matches that
horizon. Score verification and prediction-accuracy comparison remain distinct.

## Runtime invocation

With trusted external model/simulator artifacts and host-side DeepSeek credentials,
start the service:

```bash
python -m missionos_cli prediction serve-stacking-mission \
  --trusted-model "$STACK_MODEL" --model-sha256 "$STACK_MODEL_SHA256" \
  --policy-sha256 "$STACK_POLICY_SHA256" --llm-backend deepseek --llm-model deepseek-flash \
  --seed 67002 --seed 67016 --output "$STACK_SERVICE_OUTPUT" \
  --approve-simulator-mission --operator "$OPERATOR_AUTHORIZATION_REF"
```

A separately running frozen VLA RPC service and the external staged simulator
are required, as in the original stacking integration. For each approved seed:

```bash
python scripts/run_stacking_mission_e2e.py \
  --simulator-module-dir "$STACK_SIM_MODULES" --output-root "$STACK_OUTPUT" \
  --service-url "$STACK_SERVICE_URL" --policy-sha256 "$STACK_POLICY_SHA256" \
  --seed "$SEED" --allow-simulator
```

This executor refuses the legacy ungoverned service. At the first decision it
also probes missing approval binding, stale observation and changed revision;
each must be rejected before any motor call, with simulation time unchanged.
Raw LLM prompt/response records, admissions, decisions, policy reservations,
dispatches, selected action arrays and measured results form the local audit.
Do not import these private/raw artifacts wholesale into the public repository.

## Risk-semantics correction and matched rerun (R4), 2026-09-19

After inspecting R3's early stop, the user requested a correction and rerun.
Only the score-comparison evidence and judgment instructions changed. WAM, VLA,
seeds, physics, action execution and terminal scoring remained frozen. This is
one post-hoc correction on two already inspected cases, not held-out evaluation.

| Seed | Previous DeepSeek R3 | Corrected DeepSeek R4 | R4 terminal decision |
| ---: | ---: | ---: | --- |
| 67002 | 5 | 8 | Bank before placement 9; terminal hold stable |
| 67016 | 8 | 8 | Bank before placement 9; terminal hold stable |
| Total | 13 | 16 | |

At seed 67002, decision 6, the raw risks remained **0.084593 continue / 0.007822
bank**. DeepSeek now explicitly cited **5.492440 continue-then-bank proxy points
versus 4.960892 bank proxy points**, named the uncalibrated-score assumption, and
selected continue. The actual placement was stable. At decision 9 it selected
bank, citing 1.636099 continue versus 6.654698 bank proxy points; the measured
terminal score was eight.

At seed 67016, decision 9, it cited **7.024974 continue versus 7.819628 bank**
proxy points and again stopped at eight. Thus the correction did not merely
force more placements everywhere. However, both games still stopped at the
same count, and no state-dependent generalization or optimal stopping claim
follows. The previous deterministic WAM rule scored 8 and 9 on these seeds;
R4's total of 16 still does not exceed that reference total of 17.

The audit verified identical pre-decision input arrays through decision 6 for
67002 and decision 9 for 67016, matching forecast risks and poses within 1e-12,
and identical motor tapes wherever the decisions in these prefixes agreed.
The changed judgment therefore occurred with the same current state and WAM
forecast. Both the arithmetic aid and prompt framing changed together; their
individual effects are not isolated. Remote LLM variability is also uncontrolled.

The complete actual runtime audit matched **18 DeepSeek judgments, 5,112 motor
steps and 208 SmolVLA inference chunks**, plus six pre-motion rejection probes.
It checked the actual API prompt against the admitted situation, including the
score comparison, response hashes, decision/ticket/reservation bindings, motor
arrays, measured scores and terminal receipts. Both games and both host services
exited zero. Standard subprocess invocation evidence validated. No cloud GPU or
physical robot was used. The API reported 81,279 prompt and 7,130 completion
tokens (88,409 total); no currency cost is inferred.

R4 used `stacking_mission.py` SHA-256
`fc302df01264b4dbc4deff058f4fbf0a423a315959adb9a1155417317240b431`.
The production code manifest remained unchanged throughout the run. No further
prompt iterations or new seeds were run to pursue a higher score. Neither R4
game completed ten blocks. This demonstrates a corrected known-case decision
and intact governed execution, **not statistically established performance gain,
calibrated risk, or superiority over WAM plus the deterministic stop rule**.

## Previous DeepSeek validation (R3), retained, 2026-09-19

At the user's request, the final backend is **DeepSeek**, not Gemma. The available
API model list was checked and `deepseek-flash` was requested; every response
also identified `deepseek-flash`. There is no silent fallback to a local model.

| Seed | DeepSeek terminal decision | Measured score | Decisions |
| ---: | --- | ---: | ---: |
| 67002 | Bank before placement 6; terminal hold stable | 5 | 6 |
| 67016 | Bank before placement 9; terminal hold stable | 8 | 9 |

The R3 audit matched **15 actual DeepSeek judgments, 4,260 motor steps and
169 actual SmolVLA inference chunks**. Six live rejection probes rejected
invalid approval binding, stale observation and changed revision before motion.
Both simulator games and both services exited zero. Forecasts, admissions,
judgments, bounded policy reservations, tickets, action arrays and measured
terminal scores are linked in the local receipt chain. The WAM and VLA digests
and physical scenarios were unchanged.

DeepSeek stopped earlier than the previous lab stop rule in these cases. For
seed 67002 at decision 6, continue risk was 0.084593 and bank risk 0.007822.
DeepSeek cited the asymmetric cost of losing five points and larger predicted
drops. This is actual evidence-dependent rationale and action selection, not
proof of optimal expected-score reasoning or calibrated probabilities. These
results do **not** establish improved decision quality. Neither R3 DeepSeek
game reached ten blocks; the real ten-block completion below belongs to the
earlier Llama 3 run, and must not be attributed to DeepSeek.

The R3 adapter SHA-256 is
`48c77e9ebbcbbdf931ce4604d848a17c0ec794608dcdb1ac366ed264cca173dc`.
Remote weights are unavailable: `model_sha256=null` for DeepSeek rather than an
invented checkpoint digest. Response IDs, requested/returned model IDs, usage
and prompt/response hashes are retained. The 15 calls reported 60,214 prompt
tokens and 4,372 completion tokens (64,586 total). No currency cost is inferred.
DeepSeek API usage occurred; no cloud GPU was provisioned.

`DEEPSEEK_API_KEY` is supplied only to the authorized host service process. It is
not saved in artifacts, committed, or forwarded to the simulator/VLA process.
The user-facing command requires the caller to supply credentials through its
existing secret mechanism; no workstation-specific secret reference is shipped.
Tests use synthetic credentials and verify that they do not appear in saved
prompt/response records.

## Earlier local-model coverage, retained as history

| Run | Actual local Assurance model | Seed | Terminal result | Score | Decisions |
| --- | --- | ---: | --- | ---: | ---: |
| R1 | Llama 3 | 67002 | Continued through step 10; collapse measured | 0 | 10 |
| R1 | Llama 3 | 67016 | Ten placements plus terminal hold, stable | 10 | 10 |
| R2 | Gemma 4 26B | 67002 | Bank selected before placement 9; terminal hold stable | 8 | 9 |

R1 preserved a real failure: Llama 3 chose continue in all twenty judgments.
High predicted risk did not reliably change its choice. R2 was explicitly added
after this result to exercise the missing stop branch with another installed
local LLM. It is a post-hoc integration coverage check, not a pre-registered or
statistical comparison of models. No WAM/VLA training or physics tuning occurred.

For seed 67002, all nine pre-decision simulator input arrays match exactly
between runs; forecasts match within 1e-12 (maximum numeric difference
1.11e-16), and the first eight selected motor tapes match exactly. At decision 9,
continue risk is 0.818211 and bank risk is 0.168163. Llama 3 selected continue;
Gemma selected hold and explicitly cited the risk contrast and eight secured
blocks. Those different proposals reached different actual simulator actions.
The eventual scores were zero and eight. This does not isolate the correctness
of the 28.4-second ninth-placement forecast, nor establish general LLM superiority.

The combined audit matched **29 actual LLM invocations/decisions, 8,520 motor
steps and 364 actual SmolVLA inference chunks**. It checked admission and prompt
identity, model response hashes, proposal-to-dispatch binding, policy reservation
counts, action-array hashes, measured collapse/score consistency, final mission
receipts and code manifests. Nine live rejection probes (three per game) rejected
invalid approval binding, stale observation and changed revision with zero
motor calls and unchanged simulation time. Public HTTP tests additionally cover
a completely unapproved policy store, revocation, altered decisions/state,
duplicate dispatch, unauthorized observations and terminal-result ordering.

The local audit also validated `runtime_invocation_evidence.v1` for the three
actual Docker subprocesses using exit codes and captured output hashes. Their
timestamps describe the enclosing observed session interval, including startup
and queued work, not precise motor-loop latency. All three simulator processes
and both pairs of VLA/MissionOS services exited zero. No cloud GPU was used.

### Model and code provenance

- WAM SHA-256: `fac4cf0ea85e1d7eef3a0af361e842b88fbe60148003530ac00c337b4660730b`.
- SmolVLA SHA-256: `254ec40e3be4a44f62073be9cea999ed165ca533d26204beb7f1238890fd38b4`.
- Llama 3 local digest: `365c0bd3c000a25d28ddbf732fe1c6add414de7275464c4e4d1c3b5fcb5d8ad1`.
- Gemma 4 local digest: `5571076f3d70050487b26b341705799e0ab29b808164f90d20d4cf84f699d251`.

R1 used `stacking_mission.py` SHA-256
`282eb45ebffa6bc7d896320311171b7afdddb20e6e8851602c443e14676803bd`.
Before R2, the adapter explicitly disabled optional model thinking and added
rejection of a terminal-hold result arriving before its placement result. R1's
actual placement/hold ordering was already valid. R2 used adapter SHA-256
`1e1dd8ec90d0fd2006981a8bbe6847fc38db48fdf9473f932c51babfef5c4e13`.
R1 was not silently relabeled as a rerun of that later revision. The final
terminal-ordering contract is covered by tests; the final live R2 exercises the
stop path. Full per-run code manifests and raw simulator/model records remain
local rather than being imported into the public repository.

Final automated validation: **68 tests passed**, with lint, whitespace and
changed-document link checks also passing. The six-case synthetic intake CLI
smoke still passes. The relevant test command is:

```bash
PYTHONPATH=packages/missionos-core/src:packages/missionos-cli/src:. \
  python -m pytest -q tests/contract/test_assurance_prediction_evidence.py \
  tests/contract/test_mission_prediction.py tests/e2e/test_mission_prediction_http.py \
  tests/e2e/test_stacking_mission_boundary.py \
  tests/contract/test_missionos_core_action_feasibility.py
```

## Limits

The loopback receiver trusts this local simulator client. It is not an
authenticated distributed telemetry service, and client-provided motor traces
are audited against the saved arrays and process invocation record rather than
independently sensed hardware. Simulation is paused during LLM judgment and
uses an explicit 300-second lab freshness bound; real-time robot latency is not
validated. Staged grasp setup and the original VLA/controller macro limitations
remain. Technical failures or invalid LLM output end the run without a fallback
or an invented valid score.

This is known-case integration evidence. It does not demonstrate improved mission
success, calibrated risk, broad WAM superiority, or that the LLM causally improves
on a simple stop rule. Those are separate experiments.
