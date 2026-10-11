# Alternate-zone AeroVLA capacity-wait attempt

One VM was secured, but the bounded comparison did not complete. No further paid attempt is permitted in this capacity-wait group.

Guest abort, if present: {'type': 'OSError', 'reason': '[Errno 5] Input/output error', 'unix_s': 1791701979.910109}. Weights/dependencies prepared:
**True**. Partial raw responses, if any,
remain preserved and are never promoted into a completed image-pair result.

This turn submitted one create request in `us-west1-b`, after verifying the
previous `us-west1-a` failure was terminal and no VM or disk remained. Regional
L4 support, the pinned image, quota, billing and current Oregon public pricing
were checked before creation. At most one L4 VM, one synthetic warmup and one
open/blocked comparison each were permitted. No WAM, flight, child-created
schedule, retry, push, PR, merge or deployment was performed.

The same capacity-wait group retains its USD 1 budget; no additional reserve was
added for this turn. Prior conservative reserves remain USD 6, for USD 7 total
under the USD 8 authorization. The all-in conservative ten-minute estimate was
USD 0.426601, including 200GiB Balanced storage,
one ephemeral IPv4 and at most 1GiB outbound traffic, before tax and currency
conversion. This is not a provider invoice or a provider-enforced spend cap.

The exact saved RGB pair, neutral prompt, unmasked deterministic twenty-token
generation and pinned OpenVLA/AeroVLA revisions were retained from `02c21b3`.
The prepared guest enforces conservative packet quotas and absolute stage
limits: 360 seconds preparation, 90 comparison and 150 collection/deletion.
Cloud DELETE termination and an independent exact-owner local watchdog were
configured, but the unsupported local delete flag prevented early cleanup and
the 600-second bound was not met. The initial create response wait was 150 seconds; an
ambiguous response does not cause a resend or early assumption of absence.
Cloud insert must be terminal and both VM and disk absent to finish cleanup.

VM secured: **True**. Completed warmup: **0**.
Completed comparisons: **0**. Cloud insert status:
**DONE**; error codes: **none**. Resource absence was confirmed
at 2026-10-11T07:07:22.496192+00:00, 692.444 seconds
after VM creation. The cloud automatic-delete operation completed at
2026-10-11T07:06:09.553000+00:00,
619.501 seconds after creation.
**The 600-second deletion/absence condition failed.** The
local deletion commands mistakenly included unsupported `--async`; those
errors and the missed-deadline record are retained. Cloud automatic deletion
ultimately removed the instance and boot disk. The recovered guest supervisor
trace shows an I/O error writing its progress message after weights and
dependencies were prepared, before the model runner started. Raw error logs
were retrieved through 60-second expiring instance SSH access, reusing an
existing key. No permanent access or new key was created, and no preparation
or model inference was retried. The complete machine-readable record is
`yokohama-capacity-wait-vla-20261011-0650.json`.

After a VM is secured, success, abort and failure all end paid attempts in this
capacity-wait group. With no VM secured and terminal capacity failure, the parent
may continue its already authorized hourly schedule within the unchanged budget.
This child creates no cron, schedule or retry loop. CPU fixtures and source
qualification do not prove learned avoidance, delivery or operational freshness.

Evidence, raw outputs/errors, source/approval/price records and cleanup proof are
kept in restricted local storage and verified through archive and Git restores.
The source checkout and previous evidence archives are retained.

Official pricing: [GPU](https://cloud.google.com/products/compute/pricing/accelerator-optimized),
[storage](https://cloud.google.com/compute/disks-image-pricing),
[network](https://cloud.google.com/vpc/network-pricing).
