# Offline AeroVLA lifecycle repair

The secured-VM failure in `1f30d86` remains a failure: progress output to
`/dev/ttyS0` raised EIO before model startup, both SDK deletion paths used an
unsupported `--async`, and provider deletion completed after 619.501 seconds.
Original logs, budget records, archives and the paid-attempt stop marker remain
unchanged. This repair used CPU fixtures only: 125 regression cases, lint,
format checks, real local subprocess timeouts, CLI entrypoints and a separate
CPU watchdog process passed. No VM, GPU, GCP API, model API or flight was used.

## Implemented behavior

- The guest-agent startup template keeps its normal journal/output handling;
  it does not reopen a TTY. Mandatory file events, atomic status and abort
  records precede optional output. EIO on display/journal is recorded without
  interrupting work. Logs and a SHA-256 receipt are retrieved as a file archive.
- Failure remains the final status after network accounting. Failed guest
  execution exits nonzero, and an exclusive, flushed reservation prevents a
  metadata restart from repeating preparation or inference.
- The native helper uses the same atomic writer and durable readiness events.
  Model preparation and the 45-second comparison have monotonic outer limits;
  a missing/stale readiness receipt or a backwards wall-clock jump cannot
  extend the allowance. One warmup and the two saved-image comparisons retain
  their existing count guards and generation settings.
- Startup failure or finished comparison triggers SDK cleanup immediately.
  Stable SDK deletion uses `--delete-disks=boot --quiet` without `--async`.
  `all` was a valid disk option; removing `--async` is the relevant repair.
- A separate watchdog uses its own resource observations and REST DELETE
  request builder. CPU fixtures verify survival of primary SDK/observer/logger
  failure and process separation. Both paths require exact immutable resource
  identities and the owned boot disk. Pending inserts are not absence; foreign
  resources are rejected. Watchdog observations continue after its single
  deletion request. Late absence never clears a missed-deadline record.

The reusable controller provides explicitly supplied transports, rather than
silently accessing credentials. A future authorized launcher must install the
seven reviewed Python sources plus the pinned bootstrap, bind the exact source
manifest, run the REST watchdog in a separate process with separate reads, and
supply bounded SDK/REST observation/deletion transports. The old controller and
startup script in the failure archive must not be executed again. Cloud wiring,
Linux packet-rule installation and actual resource deletion have not been
requalified by these mocks.

## Conditional next single-attempt proposal

The existing USD 1 group retains USD 0.4568919543 estimated spending and
USD 0.5431080457 remaining. Existing cumulative spending estimates remain
USD 1.7512037928, with USD 7 reserved under the USD 8 authorization. No budget
or reserve was reset. Current Oregon public pricing gives USD 1.179605584/hour
for one g2-standard-16 L4, 200GiB Balanced disk and one IPv4. A 600-second
resource estimate plus conservative 1GiB egress is USD 0.4266009307 before tax
and currency conversion, fitting the remaining group budget by USD 0.1165071150.
Projected cumulative spending would be USD 2.1778047235. These are estimates,
not invoices or a provider-enforced spend cap.

Proposed absolute limits from submission: preparation 330 seconds, comparison
45 seconds, collection/deletion 225 seconds; start SDK cleanup at 375 seconds
or earlier on failure/completion, independent REST watchdog at 385 seconds,
and cloud DELETE termination at 390 seconds. Keep the final 600-second absence
criterion and network limits unchanged. Verify terminal insert, VM absence and
boot-disk absence separately. Guest packet rules retain the conservative 31GiB
RX/896MiB TX thresholds within the approved 32GiB/1GiB bounds, count pre-rule
traffic and drop non-loopback IPv6. No deadline or route criterion is relaxed.

The recent cold weights/dependency preparation took 233.094 seconds from
submission. The inherited full native-readiness estimate of about 89.651
seconds suggests about 322.744 seconds combined, leaving only 7.256 seconds
inside preparation. Its exact historical readiness timestamp was not
independently reconfirmed this turn, and these phases are from different runs.
A separately restored older native record shows 77.402 seconds from the model
process record to VLA warmup completion, which is not full health readiness.
The small pure load/warmup durations alone are not a cold preparation estimate.
Actual cold unmasked readiness and both comparison latencies remain unqualified.
Downloads, boot, hashing and model generation may exceed the proposed limits.

Cloud automatic termination can take up to an additional 30 seconds to begin,
and deletion completion has variable latency. Moving DELETE earlier creates
more margin but does not guarantee strict ten-minute deletion or hard cost
containment. Cost feasibility passes; timing feasibility remains conditional.
The hourly task remains disabled and the existing stop marker remains set.
This local repair does not authorize a paid retry or promote any fixture to
real model, avoidance, operational freshness or delivery success.

## Verification and recovery

CPU qualification covered the lifecycle, guest, native runner, paired-image,
AeroVLA adapter, grammar and local HTTP contract tests: **125 passed**. Ruff
format/check and `git diff --check` passed. Default guest and budget-plan CLIs
performed no cloud or model calls. Large raw evidence and public pricing page
snapshots remain in restricted local storage, with archive and Git restoration
verification recorded there. Source checkout and older evidence are retained.

Primary sources: [GPU pricing](https://cloud.google.com/products/compute/pricing/accelerator-optimized),
[disk pricing](https://cloud.google.com/compute/disks-image-pricing),
[network pricing](https://cloud.google.com/vpc/network-pricing),
[startup logs](https://docs.cloud.google.com/compute/docs/instances/startup-scripts/linux),
[stable delete arguments](https://docs.cloud.google.com/sdk/gcloud/reference/compute/instances/delete),
[VM runtime limits](https://docs.cloud.google.com/compute/docs/instances/limit-vm-runtime).
