# Integrated Starship mission: what is demonstrated

MissionOS separates judgment from permission and execution:

> LLM judges. Human approves. Rules constrain. Executor acts. Verifier checks.

The maintained opt-in simulation connects ascent monitoring, Booster recovery,
image-assisted deployment hold, retained-payload accounting, return replanning,
authenticated test-operator approval, symptom diagnosis and Ship return. The
same observed state and inventory pass between stages; a forecast is not used
to reset the actual simulator plant. Seeded fault probabilities are test settings,
not estimates of real incident rates.

## Historical integrated result

One registered internal-source flight on 2026-10-23 demonstrated these roles in
the same launch-to-contact mission. Jev returned 10 valid responses in 10 calls;
DeepSeek returned one in one. Thirteen payloads were retained and thirteen
released. An actual simulated propulsion reduction was observed through symptoms;
Jev requested independent tracking and selected numerical capability adoption.
The capability was applied once after the independent check. No symptom fallback
or API retry was used. Ship model contact was **3.459 m/s**, with **51.209 t** of
propellant remaining. Approval was supplied by an authenticated **test script**,
not a recorded human review. These counts describe that flight, not a general
success rate or model ranking.

The registered supervisor interval was **1,304.871 s / 1,800 s**. The full CLI,
including pre/post saved-record checking, took **2,140.761 s** and is separate.
The earlier 2026-10-22 flight's **2,143.796 s** timing miss remains a miss; its
862 s of host sleep is not retrospectively subtracted to make it pass.

This is historical simulation evidence on internal source `81c22072`, not a
flight executed on this public port. Public-data agreement remains limited;
this is not a validated SpaceX vehicle or operational flight-control system.
Booster acceptance used a **10 km development envelope**, not the earlier 5 km
criterion. Tower catch, actual human inflight review, arbitrary-fault recovery,
and three-launch daily operations are not demonstrated.

## Return qualification and ordinary permission

A separately registered saved-only check reconstructed every actuator-history
row in all four original records (three conditions). Fixed N/T/A have 28,025
rows each; live T has 28,066. Row-boundary chunks remain <=32 MiB; raw records
remain <=512 MiB and histories <=100,000 rows. All four records passed in
996.768 s under the original 1,800 s limit. This is an exact-point companion,
not a continuous domain or a refreshed legacy 39-corridor certificate.

The new permission adapter re-reads held actual state/vehicle objects, inventory,
plan and complete history before calling the owning host's permission entry.
A certificate alone supplies neither human approval nor dispatch. Historical
post-dispatch capability estimates cannot be assumed known at permission time.
The adapter is not globally installed over `/plan`/`approve`/`run`; ordinary launch
readiness still refuses the unmatched legacy qualification.

The next registered saved-only adapter verification stopped **before execution**
at 9.033 GiB free, below its unchanged 10 GiB minimum. No retry or new flight/API
call followed. Public migration changes the source inventory too: **no old
certificate or historical approval qualifies this public checkout**.

[Finite qualification summary](../assets/starship-final-chunked-qualification-20261024/results.json) ·
[Adapter preflight-stop summary](../assets/starship-current-return-admission-20261024/results.json)

## Inspect the boundaries without a model call or flight

With development dependencies and the `spaceflight-qualified` extra installed,
run the explicit fixed-response boundary smoke from the repository root:

```sh
python scripts/smoke_starship_final_runtime.py --run --output output/final-boundary-smoke
python scripts/check_starship_current_admission.py smoke --output output/permission-boundary-smoke.json
```

Use fresh output names. The first calls a real loopback Gateway and a diagnostic
child over inherited channels, while forbidding physics steps and provider calls.
The second uses explicitly scripted ownership/source/backend facts to exercise
the normal-return permission adapter. Neither command is a mission, a model
judgment, real human approval, dispatch, or qualification. Paid/live execution
remains separately registered, bounded and opt-in; do not reuse historical plans.

## Videos

The [README players](../../README.md#starship-inspired-six-degree-of-freedom-mission-simulation)
retain their original recorded 2026-10-12/14 mission context and public assets.
They are earlier illustrative simulator replays, not a rendering of a new public
flight or evidence of tower catch. The later internal tower insert depicts the
older timing-miss flight and is not relabelled as the accepted 2026-10-23 run.
