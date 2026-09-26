# Optional resident ANWM runtime

The experimental urban runner can preload the pinned ANWM once before fresh
observations. This changes model preparation and transport, not route scoring,
approval, independent constraints, dispatch, or verification. The normal Gateway
stop/reobserve adapter remains depth-only. No socket or inference HTTP service
is exposed by this worker.

## Operator sequence

Install the reviewed upstream revision, official checkpoint and VAE on an
explicitly authorized CUDA host. Independently cap and clean up billable resources.
Start the published runtime in an operator-managed foreground session:

```sh
HF_HUB_OFFLINE=1 python aerial_anwm_runtime.py \
  --worker-root "$REMOTE_ROOT" --upstream-root "$REMOTE_ROOT/upstream" \
  --checkpoint "$REMOTE_ROOT/assets/0200000.pth.tar" \
  --max-requests 3 --max-seconds 2400
```

Wait for `$REMOTE_ROOT/resident/ready.json` before capturing new observations.
Readiness is written only after the pinned code/checkpoint checks and strict model
load. It is not an inference receipt, approval, or flight result. The root is
single-use, including failed startup; a subsequent worker cannot reuse its state.
For a detached operator process, close all inherited standard streams and create
an independent session. A shell background launch that leaves an SSH channel open
can time out; never interpret that timeout as readiness.

Opt in using the existing `missionos_urban_gpu_transport.v1` local configuration:

```json
{
  "transport": "resident_ssh_tar_v1",
  "published_runtime_sha256": "<SHA256 of the explicitly published runner>"
}
```

The existing project/zone/instance/remote_root fields are also required. The
stream checks the published script and ready worker, transfers only request.json
and assets.npz, then waits for fresh, bounded result assets. It does not transfer
approval or dispatch credentials. Worker requests are limited to the existing
urban source kind, the exact assets filename and the same resolved model/code
paths and sampling steps as the preloaded runtime. Old output directories,
symlink inputs, oversized inputs, and mismatched model configurations fail.

The worker handles requests sequentially and admits at most three during its
first 2400 seconds, measured from before initialization. A request already running
may finish after that admission deadline. Use an independent VM lifetime limit.
Failures remain error records and count toward the request limit; no model retry
or alternate route is implicitly performed.

## Timing and receipt semantics

- `resident/ready.json`: worker ID, runtime/model hashes, initialization and
  checkpoint-validation/load times. `forecast_calls: 0` describes readiness.
- Per-result `resident_model_reused: true` and `resident_worker_id` identify the
  preloaded instance used for the fresh forecast request.
- `model_load_seconds: 0` means no model load during this request. It does not
  mean startup was free. `initialization_before_requests_seconds` reports the
  original preload duration; it repeats in receipts and must not be summed as
  three independent loads.
- `actual_model_calls`, `sampling_steps`, input and forecast hashes retain their
  existing meanings. Every candidate still runs the real model with the fixed
  seed; caching a forecast is not implemented.
- Host observation age includes preparation, transfer, forecast and consumption.
  Preloading only precedes this interval when fresh capture follows readiness.

A fixture subprocess smoke establishes queue/transport behavior, not ANWM
performance. Report actual GPU forecasts and PX4 outcomes separately. Image
compatibility remains uncalibrated risk; neither faster processing nor a resident
worker validates time-aligned prediction or continuous obstacle avoidance.
