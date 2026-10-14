# Booster recovery history supplied to mission judgment

The host loads reviewed catch-search and M3 water-entry evidence. Current
history records zero launch-derived supported catches. A nominal modeled
splashdown exists, but perturbation robustness is unqualified. Near-tower
initialized support, offline feasible paths, partial/unretained runs and final
position alone cannot become launch-derived catch success.

## Mission-connected DeepSeek report (prefix v4)

The user requested that the current catch report become judgment material inside
MissionOS, together with a tower CG in the mission video. This explicitly expands
the earlier minimal history whitelist for this booster decision. The model sees
reviewed public scalar trial counts, the latest negative contact/fuel result,
nominal splashdown metrics and their limitations, plus the current bounded
observation. Private raw records, credentials, source paths and hidden simulator
faults are excluded from the provider payload.

`mission_recovery_report()` builds the portable report from five reviewed assets.
The preflight authority embeds the exact report, its digest and its source-file
hashes. `starship_recovery_reports.py` keeps immutable reviewed report digests;
new snapshots require review and old entries must remain. Record verification
uses those frozen identities and the caller's approved source manifest, without
rebuilding a historical report from today's files. The standalone prefix verifier
also rejects invented or changed report facts.

Ascent and deployment retain Jev. The booster decision now uses DeepSeek, with
current observations and the frozen report. The model can express a preference
for capture or splashdown; `allowed_actions` separately describes the executable
set. Capture remains unconnected and cannot be dispatched. A rejected/missing
answer receives the independently checked splashdown fallback. Neither historical
failure nor the report is a current-state safety proof or execution grant.

The decision has a 75-second simulation expiry and a provider timeout at most
30 seconds, with two seconds reserved for application. The mailbox consumes each
request identity once, without retries. There is no persistent DeepSeek call-count
ceiling. The short recovery-objective answer uses thinking disabled and an
800-token total output limit; long orbital comparison keeps its separate policy.
The video displays the external decision summary, never hidden chain of thought.
Free-form prose is not independently verified.

`missionos.booster_answer.v2` accepts exactly action/summary and, optionally, a
1–64 character string `type` metadata field. That field is discarded and cannot
change the action or grant authority. Unknown fields, tool calls and invalid
objectives remain rejected. The receipt records the decoder version and whether
metadata appeared. The standalone non-actuating assessment intentionally retains
its earlier strict decoder; historical failures are not relabeled.

Provider judgment observed and action adopted are separate facts. A valid tower
preference is a model judgment but is not an executed catch; a rejected action
cannot supply adopted-phase coverage. Coverage is accounting, not an AI score
against scenario-encoded Rules or a claim that the model should match a fallback.

## Earlier minimal-whitelist assessment

The earlier M3 v3 booster Jev path and standalone MissionAgent contexts use
`model_history()`'s minimal whitelist: catch-success count/flag, nominal water-entry
flag, nominal-only scope and unqualified perturbation robustness. The expanded
v4 report is limited to the newly approved mission booster path. The earlier
standalone model-name compatibility setting does not change other mission points.

## Actual DeepSeek assessment

Three historical, non-actuating calls are retained. The first two requested
`deepseek-v4-flash` and failed model/choices-shape validation. The third used the
current documented `deepseek-flash` name and returned the accepted advisory
choice `splashdown`. The earlier raw responses were not retained, so their
exact model-name or shape mismatch is unknown. The official documentation
explains that the legacy Flash names route to the replacement model:
[DeepSeek API documentation](https://api-docs.deepseek.com/guides/codex).

The valid answer cites no demonstrated launch-derived catch and nominal modeled
water entry. It also says that perturbation robustness is qualified, contrary
to the supplied false flag. This prose error is preserved and explicitly
reported. A schema-valid choice is not independent verification of its reason.
There was no dispatch, new flight, approval or actuator command.

Reviewed scalars and the exact whitelisted contexts are in
`docs/assets/starship-booster-recovery-assessment-20261013/summary.json`.
The latest request has a 35-second local decision window, a 30-second provider
timeout and an 800-token output limit. There is no persistent call-count ceiling;
each explicit CLI invocation performs one request without retries. Keys are read
from Secret Manager inside the host and are never printed or persisted.

## E2E / Runtime Verification

Actual CLI invocation; private project locator normalized to `SECRET_PROJECT`:

```sh
PYTHONDONTWRITEBYTECODE=1 python scripts/assess_starship_booster_recovery.py \
 --approve-model-call --project SECRET_PROJECT \
 --output output/starship-recovery-history-assessment-20261013-03/assessment.json
```

Boundary: reviewed history → external-fact whitelist → Secret Manager-backed
host authentication → actual DeepSeek HTTPS request → strict response decoding
→ saved invocation receipt and non-dispatch assessment. Output is immutable by
CLI identity. The accepted request returned `splashdown`; actual returned model
was `deepseek-flash`. No simulator process or vehicle command was launched.

Host contracts passed (133 tests, 4 skipped), including unchanged M3 routing and
mission-director boundaries; the seven evidence contracts include provider payload checks, response diagnostics
and the booster model-name compatibility setting. Ruff and diff checks passed.
All 19 Ship qualification source hashes remain unchanged.

## Mission-connected runtime record (2026-10-14)

The source-frozen corrected flight used commit `75b335f3` (all 110 source hashes
matched that commit). Jev made six attempts; DeepSeek made two. The booster
DeepSeek answer was valid, selected splashdown and was adopted at T+137.0 s,
with the selection observed at T+137.1 s. Booster CG contact speed was
1.846707 m/s. No catch controller or arm continuation executed.

The distinct orbital-comparison DeepSeek answer was rejected under validation
category `summary`. Its raw response was not retained, so do not infer a precise
text-length or semantic cause. The existing reservation was preserved and Ship
contact-point speed was 4.608259 m/s (CG ground speed 4.556049 m/s). Record
verification passed; `orbital_comparison_case_incomplete` kept case acceptance
false. Do not turn this target-feature result into full M3 completion.

The earlier prototype booster response was rejected; one API diagnosis observed
an additional `type` field and a splashdown preference. Neither rejection is
relabeled valid by the subsequent decoder change. Exactly one corrected flight
ran; the later comparison failure was not retried. This is development evidence,
not a held-out success-rate estimate.

Actual runtime commands; private project locator normalized to `SECRET_PROJECT`:

```sh
PYTHONDONTWRITEBYTECODE=1 python scripts/start_starship_gateway.py \
 --state-dir output/starship-booster-report-mission-typed-20261014/gateway \
 --port 18887 --project SECRET_PROJECT \
 --deepseek-secret deepseek-api-key --jev-secret jev-api-key \
 --deepseek-model deepseek-flash --fixture-planner --enable-live-mission-director
PYTHONDONTWRITEBYTECODE=1 python scripts/smoke_starship_chat_gateway.py \
 --port 18887 --scenario sixdof_m3_orbital_comparison \
 --output-dir output/starship-booster-report-mission-typed-20261014/http
```

Boundary: production Gateway HTTP plan → explicit hash-bound test approval →
credential-free worker → host Secret Manager-backed providers → actual concurrent
6DOF execution → saved records → independent verifiers. Initial scenario selection
and preflight approval used a deterministic test driver, not a verified human
reviewer. Driver exit 3 records the incomplete orbital-comparison gate. Booster
judgment and water entry passed; no physical operation is claimed.

After that flight, two small review fixes add preflight refusal for report-source
mismatch and explicit false/empty fields on early verification failure. The latest
source's real Gateway plan/approval boundary passed on port 18891 without another
model or flight. The saved flight also passed the current record verifier, keeping
case acceptance false. All 19 protected Ship qualification sources stayed exact.
Claude Code Opus 5.5/high reviewed twice read-only; the second pass found no core
blocker and the two subsequent small fixes were checked locally and over HTTP.
No external final sign-off or external video review is claimed.

The [curated feature summary](../assets/starship-booster-recovery-decision-20261014/summary.json)
keeps source/record hashes, negative prototype and API-diagnosis receipts separate.
The player/MP4 contain only reviewed numerical display fields, actual actions and
external summaries. They never manufacture a catch pose, support, hidden thought
or completed comparison. Model/template changes do not change raw flight states.
