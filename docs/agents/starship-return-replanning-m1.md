# Return replanning milestone 1

## Observable completion

A launch releases its payloads. A fresh notice makes the planned recovery area
unavailable. MissionOS uses numerical tools to evaluate a bounded postponement,
continues monitoring during the wait, and updates its return plan from new
observations. Independent checks precede execution and later observations must
confirm the changed return and the original Ship contact limits.

Normal flight must retain its outcome without unnecessary intervention. A
missing model response must lead to a checked preapproved continuation, never
an unchecked return. These are three separate end-to-end cases. A model call,
record verification, or return candidate alone does not complete this milestone.

The wider goal remains flight-wide AI control and eventually repeated flights.
Numerical estimators are the AI's tools. Script parity is not an AI-value gate.
New authentication, a new control-room UI, fleet scheduling and tower catch are
outside this first milestone; existing inspection/report surfaces can show it.

## Operational basis and exclusions

[NASA's Crew Dragon return description](https://www.nasa.gov/reference/nasa-spacex-crew-launch-return-operations/)
describes repeated weather/recovery assessments and postponement or alternate
sites. This is an operational analogy, not Starship flight software, a Starship
endurance claim, or permission to copy Dragon's times and thresholds.

The existing fuel-shortage case has about 22.4 t left, below the 28 t reserve.
Waiting cannot make that case pass an unchanged reserve requirement. It remains
unresolved, not the successful postponement scenario.

The current surrogate lacks power, thermal endurance and propellant boil-off
models. They cannot be treated as infinite resources. Initial timing probes
measure only the effect of a short changed coast on the existing dynamics.
Before online postponement admission, explicit bounded assumptions or models
for relevant waiting resources, uncertainty and expiry are required.

## First gate: two full-flight timing probes

Freeze production source at `caf547f26ef53529f20d6490a1a4a04f2e457bff`.
Change only `guidance.coast_before_return_s`: nominal and nominal +60 s. Both
launch from the same physical initial state and release all 26 rigid payloads.
Use the existing opt-in development qualification path and
`trimmed_state_terminal_v4`. Do not change forces, gains, geometry, tolerances,
integration intervals or the existing qualification certificate.

Budget: two CPU flights, 900 wall seconds per flight, no automatic retry,
no hosted inference. Use the recorded NumPy 2.4.6 / SciPy 1.17.1 backend.

```sh
python scripts/probe_starship_return_delay.py --approve-simulation \
  --delay-s 0 --output-dir output/return-delay/nominal
python scripts/probe_starship_return_delay.py --approve-simulation \
  --delay-s 60 --output-dir output/return-delay/delay60
```

Output directories must not exist. Preserve inputs, source copies, full study,
verifiers, final contact coordinates/time, and the result even on failure.
Process exit 0 means source and record checks passed, **not** successful return.
The separately reported `contact_limits_met` requires orbit, 26 releases,
contact speed <=5 m/s, tilt <=5 degrees, rate <=0.02 rad/s and fuel >=28 t.

Compare the complete physical sample prefix before the nominal return request
and all payload-release records. Measure the contact-location/time shift rather
than assuming the delayed flight reaches the original recovery area. Neither
probe certifies an area, water entry, waiting endurance or an online decision.

If this first gate fails, diagnose the saved samples and identify the control,
trajectory or runtime limitation. Do not turn it into an unlimited parameter
search or hide the failure behind successful log validation.

## Work after the first gate

1. Provide a resumable prediction state with finite actuator/controller history.
   An offline launch replay is not a rollout from a current observed state.
2. Evaluate bounded return-time candidates from observations/estimates, without
   giving the policy future faults or plant truth. Include fuel, duration,
   contact footprint, uncertainty, resource bounds and validity limits.
3. Separate reproducibility of the six-DOF forecast from independent constraint
   checks and uncertainty/numerical-error assessment. Repeating the same model
   does not validate the model or detect its shared implementation errors.
4. Connect Jev's tool requests, wait, new observation and revised plan to the
   existing authority/execution boundary. DeepSeek is conditional on actual
   need; invocation count is not the acceptance criterion.
5. Advance the plant clock while tools/models are pending; bind results to plan
   revision and state, then recheck at dispatch. Reject stale results and
   out-of-scope changes. The old 39-trajectory matching envelope stays intact.
6. Run normal, changed-recovery-condition and missing-response E2Es; distinguish
   fixtures from actual inference and preserve unresolved/failing outcomes.

The two-flight gate passed after re-reading the saved JSON artifacts. Both
original processes reported invalid JSON because the first probe version passed
in-memory tuples to the verifiers. No dynamics were repeated. Original verdicts
and executed source hashes remain in the audit. Contact shifted by about 278 km;
no recovery area or wait-resource admission was demonstrated.

Current implementation and evidence status are recorded in the
[timing-probe report](../examples/starship-return-replanning.md). Completion of
the two-flight gate does not close M1.
