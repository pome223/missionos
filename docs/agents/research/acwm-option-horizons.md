# Experimental two-option ACWM adapter

`StackingACWMOptionsPredictor` accepts an explicitly supplied neural-video backend
and pinned model, visual-readout, and policy digests. It produces two forecasts
from the same validated current image/state, before any placement execution:

| Option | Future covered | Horizon |
| --- | --- | --- |
| continue | One registered placement followed by a terminal hold | 28.4 seconds |
| bank | Registered noncontact stop and hold | 14.2 seconds |

Both options are required. The current-input whitelist remains the original ACWM
contract; future action tapes, future frames, and outcome labels are rejected.
Each backend call receives its own copy of the same current arrays. An invalid
or unavailable branch makes the complete forecast unavailable.

The visual scores are uncalibrated classifier outputs. The Assurance prompt
cites both scores and horizons without converting them to expected points.
Forecast evidence supplies no recommendation or execution authority. The existing
human pre-authorization, Rules revalidation, tickets, and observation binding
still apply. Jev remains opt-in; this adapter does not change the selected judge.

A 14.2-second placement observation alone cannot verify a 28.4-second continuation
forecast. The additional terminal hold must be observed for that comparison.
The actual placement remains the existing 284-step macro; the longer forecast
does not silently lengthen every executed placement.

## Verification scope

The contract tests and loopback HTTP tests cover both branches entering Assurance,
future-field rejection, model/option/horizon mismatch, failure of either branch,
and dispatch rejection for stale or mismatched approval/state. The HTTP tests use
synthetic forecasts and judge responses; they do not establish neural prediction
accuracy, actual LLM invocation, or simulator game performance.

This adapter is experimental and has no default service startup. Qualification
of a trained two-option generator is separate from these interface tests.
