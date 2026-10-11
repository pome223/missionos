# One 900-second offline AeroVLA attempt

The single create request failed with **Google Cloud HTTP 503: Internal error**.
No allocated VM, boot disk, external address or matching insert operation was
observed. No model weights were downloaded and warmup, saved-image comparison,
WAM and flight counts are all zero. This is a failed attempt, not a model result
or a capacity-exhaustion diagnosis. No request was resent and no new trial is
authorized by the remaining monetary balance.

The request began at **2026-10-11 14:14:41.874 UTC**; its error receipt was recorded
around **14:14:44.728 UTC**. The final absence deadline was **14:29:41.874 UTC**.
SDK and independent REST observations remained empty throughout reconciliation.
Resource absence was confirmed at **14:29:14.163 UTC**, inside the 900-second
window. After the request's cloud-deletion guards had elapsed, only the exact
owned controller/watchdog processes were stopped. A later **14:32:14.482 UTC**
check confirmed zero VM, disk and reserved-address resources and no active owned
monitor process. The separate resource-check task also reported target absence
at 14:26:50 UTC; its record remains with the parent task.

HTTP 503 supplied no accepted insert-operation receipt. We preserve that
limitation rather than claim a nonexistent operation reached DONE. No deletion
operation was issued for an unallocated or unproven resource. The original
controller's no-operation observation loop required external closure after the
independent final resource check; that closure and process identities are kept
in restricted local evidence. The watchdog and normal delete transports were
qualified with CPU mocks/read-only API checks, but no real DELETE was needed.

The prepared source is `a6cd31c`, based on the `7da807e` lifecycle repair.
Pre-submission qualification passed **141 CPU/mock cases**, lint, format, default
CLI entrypoints, startup syntax, installed-SDK read flags and an independent
read-only REST boundary check. One host invocation first stopped before create
reservation because system Python lacked PIL; the qualified Python environment
was then used. That stop consumed no VM request. The single actual create
command consumed this approval even though the cloud request failed. No new
full-suite test or old evidence archive was repeated for result finalization.

Current pricing qualified the 15-minute request at **USD 0.409901396**, including
one g2-standard-16 L4, 200GiB Balanced disk, ephemeral IPv4 and at most 0.5GiB
outbound traffic. A separate 20% tax/FX contingency raised its quote to
**USD 0.4918816752**, within the preserved **USD 0.5431080457** remainder. Observed
allocated resource usage for this attempt is zero; provider billing is not
confirmed. Existing estimated spending remains **USD 0.4568919543** in the USD 1
group and **USD 1.7512037928** cumulatively under USD 8. Existing reservations
remain USD 7; no budget, reserve or historical failure record was reset.

The hourly task stays disabled, the prior stop marker remains set, and this
single-attempt approval has zero remaining attempts. Original archives/source
checkouts remain intact. The raw error, SDK/REST checks, prepared source hashes,
approval, budget, process closure and no-operation reconciliation are retained
locally with a small new evidence archive and an incremental Git bundle.

Related contracts: [900-second profile](yokohama-offline-vla-900s-profile.md),
[original lifecycle repair](yokohama-lifecycle-repair-20261011.md).
