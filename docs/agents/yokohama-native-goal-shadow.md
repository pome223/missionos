# Native goal shadow: one recorded observation pair

Status: protocol and mock-HTTP qualification only. This boundary reuses the
native AeroVLA/ANWM request path on frozen recorded observations with dispatch
disabled. Adding or testing it does not start a model, provision a GPU, execute
a flight, or establish native goal-reaching capability. A real model-only run
requires a new, separately approved execution and resource plan.

## Scope and authority

The proposed native test has one input pair: a retained D1 VLA capture and its
newer WAM capture from the same recorded stationary scene. Freeze the exact
capture manifests, raw sensor/pose files, source revision and model identities
before execution. Use the existing Yokohama capture validator; the historical
ship preparation CLI expects a different capture format and is not a substitute.

Submit at most one VLA request. If it produces a valid bounded proposal, submit
at most one WAM request for two immutable candidates: hold and that proposal.
Malformed, rejected, timed-out or unbound output ends the session; it does not
authorize a retry, replacement candidate, another capture, or another run.
Preserve the original request/response evidence and unsuccessful outcomes.

The shadow attaches to externally owned services through the native host's
read-only health/identity admission. It does not invoke their lifecycle start
or stop commands. It issues no permit, Executor command, MAVLink message or
simulator operation. Its outputs remain recorded-observation proposals and
image-consistency results, even when every check passes. They cannot be loaded
as a live decision or retroactively change the recorded flight.

The API is [yokohama_native_shadow.py](../../src/runtime/yokohama_native_shadow.py):
`preflight(plan_path)` validates locally without HTTP;
`run_shadow(plan_path, output_dir, approved=False)` requires explicit approval
before attaching. The plan binds source, capture rows, the frozen map/goal and
full expected service identities. The explicit `mock_http` and `native_shadow`
modes keep doubles distinguishable from native inference. Use the implementation
and its tests as the plan-schema reference; do not create a partial hand-written
plan that omits these bindings.

The default CLI is local preflight only:

```sh
python -m scripts.yokohama_native_shadow --plan PLAN
```

After a separately approved session with healthy externally owned services:

```sh
python -m scripts.yokohama_native_shadow --plan PLAN \
  --approve-shadow --output-dir NEWDIR
```

The second command can invoke real models in `native_shadow` mode. It is not
part of the current mock-only qualification. Local revocation closes the
shadow's authority/session state; it does not stop or delete external resources.

The existing `yokohama_native_trial.py --execute` path must not be used for this
test: it proceeds to the native flight workflow. Its `--preflight` also performs
an owned Docker probe and cloud inventory/image/quota queries. Default
`--plan PLAN` description and the vehicle service's `--validate-only` admission
are local checks, but an existing flight plan is not approval for this shadow.

## Bounded model and transport contract

The application budget is one VLA request and one WAM request, yielding at most
two WAM candidate forecasts. Each model exchange has a 75-second wall limit;
the complete attached HTTP session has a 180-second wall limit. All failures
stop; there are no retries or fallback providers. The external owner may perform
at most one separately authorized and separately counted AeroVLA synthetic
startup warmup. That warmup is real GPU computation, not a recorded-observation
request, and must appear in the final invocation ledger.

Use the pinned sources, base weights, adapter identities and helper hashes from
[native admission](ship-native-model-admission.md) and the
[motion-v4 profile](yokohama-native-flight.md#opt-in-motion-v4-mission-integration).
In particular, ANWM keeps its 16-frame history, seed 42, 250 diffusion steps,
motion-v4 adapter and model time index 1. AeroVLA keeps the declared compact-city
translation grammar. A health/config/source mismatch rejects before inference;
no automatic dependency, image, weight or model substitution is allowed.

The pair is historical, not contemporaneous with the replay wall clock. Only
the declared VLA history and newer WAM history may enter their respective
requests. Future outcome images, actor schedules, obstacle truth, expected
answers and fixture scenario labels remain excluded. Retain the original
sensor times and camera/ego transforms; do not synthesize fresh timestamps.
Freeze the approved goal and immutable VLA endpoint before preparing WAM.

## External lifecycle conditions for a future GPU run

The adapter does not own the GPU resource. An explicit external owner must own
service launch, SSH tunnels if used, process shutdown, VM/disk cleanup and the
final resource inventory. Require an independent cloud-side deadline before
model startup; a desktop `finally` block alone is insufficient. The owner must
retain a single-use reservation before allocation, exact resource identities,
the terminal create operation and cleanup receipts. Unknown create or cleanup
state blocks another attempt.

A conservative proposed envelope reuses the previously qualified single-GPU
shape: one on-demand `g2-standard-16` in an explicitly selected `us-west1` zone,
one L4 with 24 GB VRAM, 16 vCPU, 64 GiB RAM, a 200 GiB auto-delete
`pd-balanced` boot disk and one ephemeral IPv4 address. Both models must return
weights to CPU between requests. Do not allocate a second GPU or retry in
another zone after capacity or startup failure.

The external create plan must set an absolute termination time with instance
termination action `DELETE`, auto-delete boot disk and automatic restart
disabled. Record and verify the provider-returned values before model startup;
do not reset the deadline when a desktop or service restarts.

Reserve at most 600 seconds for pinned weights/bootstrap and 300 seconds for
service cold start, within an absolute 60-minute resource lifetime. The
180-second HTTP session begins only after the external owner has established
healthy services and the cleanup deadline. Preserve at least five minutes for
cleanup; success may end and delete resources much earlier. An actual request
timeout must also stop server work through the external watchdog, because
client disconnection alone does not prove GPU computation ended.

The service health/response may show zero CUDA allocated bytes after CPU
transfer; that does not prove process exit, zero CUDA-context memory, zero
power or cleanup. Require remote model-process and GPU compute-process absence,
followed by VM and boot-disk absence. Report these separately from model output
validity. This document does not establish current cloud inventory or available
quota/capacity.

## Proposed funding and price assumptions

A future run needs a new USD 2 incremental planning reservation tied to its
single immutable plan. This is separate from any Jev/DeepSeek evaluation grant
or previously consumed GPU attempt. It neither reuses their authorization nor
asserts an unspent account balance. No paid execution is authorized merely by
this document.

The estimate must include the full 60-minute instance, disk and IPv4 lifetime,
plus a conservative 1 GiB internet-egress allowance. It assumes Linux,
on-demand pricing, no discounts/credits, no snapshots or retained disks, and no
other billable resources. Prices and the chosen region must be rechecked before
approval; the definitive estimate belongs in the frozen execution plan.

The public Google pages were checked on 2026-10-03. The provisional arithmetic
is:

| Resource | One-hour planning amount (USD) | Public source and verification limit |
| --- | ---: | --- |
| `g2-standard-16`, including one L4 | 1.147208384 | [Accelerator pricing](https://cloud.google.com/products/compute/pricing/accelerator-optimized) indexed the complete machine rate; the current Oregon selection was not independently reproduced. |
| 200 GiB `pd-balanced` | 0.027397200 | [Disk pricing](https://cloud.google.com/compute/disks-image-pricing) displays 0.000136986/GiB-hour; the fetched table defaulted to Iowa. |
| One in-use standard-VM IPv4 | 0.005000000 | [Network pricing](https://cloud.google.com/vpc/network-pricing?hl=en) displays 0.005/hour. |
| 1 GiB egress allowance | 0.230000000 | The same network page displays 0.23/GiB for the high-cost China destination; this is a conservative allowance, not a verified Oregon-to-client quote. |
| **Provisional total** | **1.409605584** | **About 0.59 below the proposed USD 2 reservation.** |

The values agree with the previous single-L4 planning envelope, but the fresh
public lookup did not complete a region-specific Oregon quote. Keep pricing
admission unresolved until the exact chosen region and transfer destination are
verified; do not label this table an approved current-region estimate. Current
quota, resource inventory and account balances were not queried.

Application response/evidence size bounds do not cap all VM egress, package
downloads, SSH traffic or provider billing. Tax, account-currency conversion,
failed deletion and unbounded external-owner traffic can exceed an estimate.
USD 2 is a planning allocation, not a guaranteed billing ceiling. If a strict
provider-wide ceiling is required, execution remains blocked until an approved
enforcement mechanism exists.

## What this does not close

The CPU [observed-goal loop](ship-urban-goal-loop.md) travels 12 m with at most
three legs. The native compact-city grammar permits about 1.02–2.96 m forward
translation per proposal; three native steps cannot establish that same goal
contract. A future native loop needs a reachable approved goal and a reviewed
request/leg budget, rather than silently broadening decoder limits.

ANWM predicts candidate-view images. Existing image-consistency bounds do not
produce a calibrated collision probability or the fixture contract's boolean
`predicted_clear`. Native closed-loop admission still needs an independently
reviewed static mapped corridor, capture/freshness policy, geometric Rules,
world-to-executor mapping and observed arrival/hold evidence. Model time
calibration and dynamic-scene validity remain unverified. A successful shadow
pair proves only that the frozen native protocol and model outputs were
measured; it does not establish obstacle avoidance, goal arrival, flight,
delivery, increased autonomy or energy savings.
