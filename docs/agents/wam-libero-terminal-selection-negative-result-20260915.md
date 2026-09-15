# Why a matched LIBERO world-action model did not improve terminal selection

## Abstract

We tested whether a world-action model (WAM) value estimate, and then an
outcome-memory selector informed by prior simulator executions, could improve
the choice between two executable robot actions. The test used the released
NVIDIA Cosmos Policy LIBERO checkpoint, its corresponding LIBERO simulator,
and paired branches from exactly the same pre-action state. This was an
**offline simulator experiment**, not a MissionOS-approved dispatch or a
physical robot run.

The first completed pair contained a measurable outcome difference: both
actions succeeded, but the action with the higher model value finished **91
steps later** than the fixed no-value choice. A second, frozen batch executed
16 training branches across eight starts. All 16 succeeded, and the two
actions within each start differed by at most **three steps**. An
experience-memory ranker was fitted, but the five reserved evaluation starts
were not run to terminal outcome. The first reserved start stopped after
pre-execution selection because of a test-adapter variable collision.

Consequently, neither terminal success improvement nor a reproducible
WAM-specific speed improvement was demonstrated. The strongest measured
explanation is **insufficient choice-relevant outcome variation in the
training candidate pairs**. The experiment does not establish that WAMs are
generally ineffective, nor that the published checkpoint's value estimate is
incorrect for its own reward objective.

## 1. Question, model, and scope

The target question was not whether a model can generate a plausible future
image. It was whether future/value information changes the choice of a
*feasible action*, and whether that changed choice improves the **observed
terminal task outcome** compared with an unchanged baseline.

We used [NVIDIA Cosmos Policy LIBERO Predict2 2B](https://huggingface.co/nvidia/Cosmos-Policy-LIBERO-Predict2-2B),
with source revision `18a2accadf4e7a3531e56754102af5a24d2316da` of
[NVlabs/cosmos-policy](https://github.com/NVlabs/cosmos-policy) and model
revision `cb689ec0e3347c13667d70a78a3447388f5c3bb8`. The model card
describes a 16-step, 7-dimensional action chunk, predicted future
observation, and a scalar **expected cumulative reward from the future
state**. We used this checkpoint rather than transfer an earlier ALOHA
experience-updated model into LIBERO. Its model weights were not changed in
the experiment reported here. The outcome-memory ranker described below is
our separate research component, not a new NVIDIA checkpoint.

The simulator was [`libero_10`](https://github.com/Lifelong-Robot-Learning/LIBERO),
task 5 (placing a book in the back compartment of a caddy), using its
default initial states. The official task-completion signal, rather than a
predicted image, provided the terminal success label. All action effects
reported here occurred only in local or GPU-hosted simulation.

The model card reports high average LIBERO policy success across its
benchmark suites. That publication result does **not** show that our
two-candidate value choice, outcome-memory method, or a MissionOS response
improves over the policy. Nor does our small experiment reevaluate the
published benchmark.

## 2. Frozen causal comparison

### 2.1 Branch construction

We fixed the source and checkpoint revisions, task, initial-state indices,
policy candidate seeds (`195` and `196`), 20 Hz control rate, and terminal
budget before reading outcomes. After ten no-op stabilization steps and a
64-step seed-195 policy prefix, each candidate was a separate model-generated
16-step action chunk. Each branch was restored from the same complete
MuJoCo state and current camera/proprioceptive observation, applied its own
chunk, and then used the same ordinary policy-continuation procedure until
task success or the 520-step cap.

The branch-entry observation, proprioception, and full simulator-state
bindings had to match the saved pre-execution binding. Candidate actions,
their digests, model values, and selector choices were recorded **before**
either branch's future could be observed. A local CPU simulator smoke first
checked exact replay equality using diagnostic controls; that smoke did not
test Cosmos candidates or terminal success. The completed GPU pair then
checked same-state equality for its two real-model branches.

This construction limits state- and observation-confounding between the two
simulator futures. It does not turn two seeds into independent scenes, nor
make the candidate actions equally useful for selection.

### 2.2 Comparators and predeclared decision rule

The first pilot compared a fixed no-value choice (seed 195) against the
higher joint-sample model value. Seed 195 is a **deterministic default**, not
an independently trained current-state decision maker.

For the experience batch, training starts were fixed as initial states
`0–7`; reserved evaluation starts were `8–12`. The previously observed
pilot start `45` was neither a training sample nor a scored test start. Four
selectors were to be compared on every reserved start:

| Selector | Information used | Intended role |
| --- | --- | --- |
| Fixed default | Current executable candidate, always seed 195 | No-value baseline |
| Base model value | Candidate-associated Cosmos value | WAM-only comparison |
| Experience only | Current proprioception, action summaries, measured prior outcomes | Experience ablation |
| Experience + value | Same experience method plus candidate Cosmos value | Incremental WAM test |

The experience selector used four nearest measured candidates with fixed
feature scales and `exp(-distance²/2)` weights. It included current
end-effector position and gripper state, action translation/control
summaries, and, only in the last arm, the candidate's model value. A
nearest-neighbor distance above `3` required fallback to the fixed default.
Otherwise a predicted success difference of at least `0.05` took priority;
with smaller success differences, seed 196 needed a predicted advantage of
at least ten completion steps. These parameters were not fitted against
reserved test outcomes.

The predeclared WAM-specific publication gate required preservation of
terminal success against all three comparators; at least 10% fewer total
steps than the fixed and base-value arms; at least 5% fewer than experience
alone; faster completion than the fixed arm on at least three of five starts;
no individual regression greater than 20 steps; and at least two choices
different from experience alone. Passing these one-task internal gates
would still have required a separate MissionOS authority/runtime and
publication review before any production selector could be adopted.

## 3. Execution history and observed results

### 3.1 Environment and adapter failures before the measured pair

The experiment encountered several *our-side* setup and adapter failures.
They are not model-quality observations:

1. The first allocated GPU environment could not build a transitive
   dependency because CMake was absent. No model inference occurred.
2. A later environment downloaded the pinned checkpoint but its CUDA/Triton
   import failed without the Python 3.10 development header (`Python.h`).
   Again, no candidate was generated.
3. After those dependencies were repaired, the checkpoint loaded with no
   missing, unexpected, or shape-incompatible model weights. Our adapter
   nevertheless invoked a helper that the official serial Cosmos call path
   does not invoke. That helper expected `model.norm_stats`, which this
   model instance did not expose, and stopped before candidate inference.

We removed the extra call and added preflight checks. Only the later pilot
produced actual model candidates and terminal branches. Model loading must
not be counted as an action/value result; conversely, these environment
failures must not be counted as evidence that the model cannot predict.

### 3.2 Completed pilot: initial state 45

Both candidates were generated from the identical current observation and
state. Their action digests differed; the 16-step action-chunk L2 distance
was `0.2646944`, and their end-effector positions after the candidate chunk
were approximately `0.009597 m` apart. The candidate values and choices
were saved before branch execution.

| Candidate | Model value | Terminal success | Steps after stabilization | Choice |
| --- | ---: | --- | ---: | --- |
| Seed 195 | `0.2516813874` | Yes | `289` | Fixed no-value default |
| Seed 196 | `0.2545773983` | Yes | `380` | Higher-value choice |

The value gap was `0.0028960109`. The higher-value choice took **91 more
steps** (4.55 s at 20 Hz). There was no success-count difference in this
pair. A higher cumulative-reward estimate need not minimize completion
time: the model card does not establish that step count is the value's
training target or that these values are calibrated completion
probabilities. Thus the observation is *failure to improve this terminal
comparison*, not proof that the value function was mathematically wrong.

Contact/collision observations were not instrumented for this pilot; their
status is unknown, not collision-free. The 91-step result concerns
simulated task completion after candidate action plus continuation, not a
verified safety or physical outcome.

### 3.3 Frozen experience batch: training starts 0–7

The next GPU batch executed both candidates to terminal outcome at each of
the eight fixed training starts. Every candidate achieved the LIBERO task
success signal. Completion steps were:

| Start | Seed 195 | Seed 196 | Absolute pair difference | Base value choice |
| ---: | ---: | ---: | ---: | ---: |
| 0 | 166 | 167 | 1 | 195 |
| 1 | 162 | 165 | 3 | 195 |
| 2 | 177 | 177 | 0 | 195 |
| 3 | 164 | 164 | 0 | 196 |
| 4 | 189 | 191 | 2 | 195 |
| 5 | 175 | 177 | 2 | 195 |
| 6 | 179 | 178 | 1 | 195 |
| 7 | 185 | 185 | 0 | 195 |

The maximum within-start difference was **three steps**, or 0.15 s at
20 Hz. No training start had a candidate-success split or met the
predeclared **20-step informative-pair threshold**. The ranker fitted to
these 16 measured outcomes changed only its experience memory; it did
**not** update Cosmos weights or demonstrate improved future prediction.
Across these eight *training* starts, always choosing seed 195 took
`1,397` steps in total. An oracle allowed to see both terminal outcomes
and choose the better candidate within each start would take `1,396`:
only **one step**, or approximately **0.072%**, less. This is an upper
bound for the two-candidate choice on the measured training panel, not
an estimate of held-out or other-task performance. Even perfect ranking
could not meet a 5–10% speed-gain requirement on these pairs.
Because the labels were nearly constant, fitting it did not demonstrate
that experience can identify a meaningfully better action on unused
states. The pilot's 91-step difference remains a useful diagnostic from
a *different* start; it cannot supply variation to these fixed training
pairs without changing the frozen protocol.

### 3.4 Reserved evaluation: incomplete, not negative

At the first reserved start (`8`), all four selectors recorded seed 195
before candidate execution. The experience arms recorded an
out-of-range fallback. The run then stopped **before either reserved
branch's terminal outcome**: our test adapter had assigned the JSON
ranker dictionary to `model`, replacing the Torch model reference used by
policy continuation. It failed with `AttributeError` on `model.config`.

The variable collision was fixed, and a preflight now rejects recurrence.
Nevertheless, **no terminal evaluation receipt exists for start 8 or
starts 9–12**. We did not launch another GPU evaluation against this
already label-collapsed training matrix. A subsequent admission check
requires outcome divergence on at least two distinct training starts
(different success or at least 20 steps) before a frozen experience
selector may be evaluated. That check is a future fail-closed rule; it did
not retroactively alter the saved test-8 choices.

No claim about held-out performance, success preservation, incremental
WAM value, or generalization is justified by pre-execution choices alone.

## 4. Why added value was not demonstrated

The explanations below are separated by what the data actually establish.
They should not be flattened into either "model failure" or "bad research
design" without further tests.

| Explanation | Supporting observation | Status and limit |
| --- | --- | --- |
| **Candidate outcome collapse** | All 16 training branches succeeded; each paired completion difference was 0–3 steps. | **Observed.** The measured data offered virtually no counterfactual choice signal for this objective. |
| **Task/policy saturation** | The matched LIBERO policy succeeded on every training candidate; the released model reports high LIBERO benchmark success. | **Plausible, not isolated.** Eight starts of one task cannot locate the cause across tasks or policy checkpoints. |
| **Seed changes are not task-different interventions** | In the training starts, seed 195/196 produced different chunks but not consequential terminal outcomes. | **Observed for these pairs only.** Different action tensors are not evidence of useful action alternatives. |
| **Value/objective mismatch** | At start 45 the higher value selected an equally successful but slower branch. | **Possible.** Value estimates cumulative reward, not documented steps-to-success; no reward-ground-truth comparison was made. |
| **Outcome-memory representation or thresholds** | The ranker used short-horizon action/proprioception summaries, a four-neighbor kernel and fixed fallbacks. | **Possible, untested.** The near-constant training labels and missing held-out outcomes prevent attribution to features or hyperparameters. |
| **Evaluation adapter bug** | At reserved start 8 the Torch model was overwritten by a dictionary before policy continuation. | **Confirmed for the incomplete evaluation**, not a model-prediction failure. Correcting it cannot manufacture missing choice-relevant training labels. |

The root issue for this *particular selection test* is measurable before
testing another selector: the actions offered to it almost never changed
the terminal objective. If two actions lead to the same success and nearly
the same time, even a perfect ranking signal can show at most a tiny gain.
This explains why simply adding experience or a value feature was not a
credible route to the predeclared 5–10% gains here. It does **not** mean a
future model cannot help choose between genuinely different actions.

The pilot at start 45 prevents an overly broad "all candidates are
equivalent" conclusion: there, continuation produced a 91-step difference.
It also warns against treating small first-chunk end-effector differences
as a complete summary of downstream effects. With only one such pilot,
however, we cannot estimate how often that divergence occurs, whether
model value ranks actual reward, or whether selecting the faster candidate
would improve mission success rather than only time.

Our two-seed procedure was also **not a reproduction of a published
multi-candidate, multi-rollout planning result**. A checkpoint's policy
benchmark and its planning/value research are separate evidence. Further
seed or setting search after observing these results would introduce
selection bias; it was not done.

## 5. Integrity, authority, and publication boundaries

The experiment saved pre-execution candidates/choices separately from
terminal receipts and checked state/action bindings. Result bundles passed
SHA-256 manifest verification before cleanup. Those internal generated
bundles, checkpoint files, tokens, private cloud identifiers and local
workstation paths are **not included in this public report**. The text is
a reviewed aggregate of measured outcomes, not a portable evidence bundle
or a reproduction of a live MissionOS workflow.

Five allocated GPU attempts in this sequence (including setup failures,
pilot and experience batch) had a conservative combined resource-cost
estimate of approximately **USD 0.80**. This is not an invoice: ancillary
charges and local workstation work were not fully priced. Each owned GPU
instance and its boot disk were absent at post-run inventory checks. No
unrelated resource was changed. Cost and cleanup establish experiment
scope, not WAM efficacy.

No MissionOS operator approval, Rules dispatch revalidation, Executor
invocation, Verifier-confirmed MissionOS mission completion, controller ACK,
or physical robot execution was created in these experiments. The
simulator's task-success signal is an observed **LIBERO branch outcome**,
not an approval or production mission claim. The authority boundary
remains: LLM/model judges; human approves; Rules constrain; Executor acts;
Verifier checks; Repair loops.

## 6. Decision and bounded next test

Do **not** ship a WAM candidate selector or claim improved mission success
from these results. Retain the negative result and the distinction between
the confirmed candidate-label collapse, the one slower higher-value pilot,
and the unmeasured held-out comparison.

A next test should first preregister a harder, simulator-replayable state
and at least two **policy- or planner-generated executable alternatives**
whose *measured terminal task outcomes* actually differ. Candidate
generation must not use post-outcome cherry-picking, artificial HOLD, or
simulator future data as a runtime input. Only after finding repeated
choice-relevant training pairs should current-only, experience-only,
base-WAM and experience+WAM be compared on separately fixed unused starts.
Freeze candidate generation, model revision, terminal predicates, budget,
adoption rule, and retained failures before that evaluation. If no such
alternatives exist, candidate generation or the intervention space is the
next product problem—not more value-model tuning.

Even a favorable offline result would not authorize an operational
MissionOS response. Public adoption would still require a bounded,
backend-neutral integration and a runtime test of human approval, Rules
constraints, Executor effect, and Verifier outcome, with simulator and
physical claims kept distinct.
