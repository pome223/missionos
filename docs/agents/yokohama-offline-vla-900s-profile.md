# Single approved 900-second offline AeroVLA profile

The repair in `7da807e` remains the baseline. A later explicit user approval
allows one additional VM attempt only when its current estimate fits the
remaining USD 1 group and cumulative USD 8 budgets. Historical failure evidence,
spent amounts, the earlier stop marker and the disabled hourly task are retained.
This is a new single-attempt approval, not permission for automatic retries.

The reviewed guest profiles are 600 seconds (330-second preparation and
375-second comparison end) and 900 seconds (420-second preparation and
465-second comparison end). Both limit comparison to 45 seconds from readiness.
For the 900-second profile collect file evidence and guest startup journals by
485 seconds; start normal deletion at or before 485, independent REST watchdog
at 510 and cloud DELETE at 540. The final resource-absence criterion is 900
seconds from submission. The remaining 360 seconds after cloud DELETE provides
more completion margin; it is not a provider completion guarantee. Late deletion
remains a failed timing condition and cleanup continues for the same resources.

The lower 900-second transfer budget is 32GiB ingress and 0.5GiB egress, with
conservative guest thresholds of 31GiB RX and 448MiB TX including pre-rule
traffic. Non-loopback IPv6 is dropped. Existing route, 8m goal, neutral prompt,
unmasked deterministic 20-token generation, pinned weight revisions and count
limits remain unchanged: at most one synthetic warmup and one comparison for
each saved open/blocked image. No WAM or flight is allowed by this profile.

At the 2026-10-11 review, one g2-standard-16 L4, 200GiB Balanced disk and one
ephemeral IPv4 cost USD 1.179605584/hour. The 900-second estimate with conservative
0.5GiB egress is USD 0.409901396. A separate 20% tax/FX contingency raises this to
USD 0.4918816752, below the preserved group remainder USD 0.5431080457. Prior
group spending remains USD 0.4568919543, cumulative spending USD 1.7512037928,
and conservative reservations USD 7 under USD 8. This is not an invoice,
confirmed tax rate, new budget allocation or provider-enforced dollar cap.
The pinned Deep Learning VM image has no additional image fee. No new service
account, bucket, static address, key or permanent authentication is required.

`scripts/yokohama_offline_vla_cloud.py` supplies bounded SDK reads/deletion and
independent REST reads/deletion. It performs no automatic provisioning or
credential discovery. The generic
SDK command wrapper executes caller-supplied commands; separate creation
approval remains the caller's responsibility. Credential-bearing auth commands
are handled separately in memory rather than passed to its receipt logger. Its
REST token comes from the authorized controller in memory;
headers and tokens are never logged. Only GET and DELETE to Compute Engine are
accepted. The caller launches a separate watchdog process and passes its token
through stdin, with independent resource observations and immutable ownership
binding. The old faulty controller is never replayed. Metadata startup uses the
guest-agent journal without a TTY redirect. File hashes support Ubuntu Python
3.10; they do not require Python 3.11's `hashlib.file_digest`.

The qualified host controller remains in restricted local evidence because its
account-specific configuration and raw receipts do not belong in public Git.
Before VM creation, CPU/mocked transports, time/count/cap guards, startup syntax,
CLI defaults, SDK flags and a read-only REST check against historically deleted
resources are required. Runtime source hashes, real GPU/model identity, file
archive receipts, operation finality, VM/disk/IP absence and final process
closure must be verified separately. CPU fixtures and this profile do not prove
real inference, learned avoidance, operational freshness or delivery success.

Official references: [GPU](https://cloud.google.com/products/compute/pricing/accelerator-optimized),
[disk](https://cloud.google.com/compute/disks-image-pricing),
[network](https://cloud.google.com/vpc/network-pricing),
[Deep Learning VM fee](https://cloud.google.com/deep-learning-vm/pricing),
[delete arguments](https://docs.cloud.google.com/sdk/gcloud/reference/compute/instances/delete),
[runtime limits](https://docs.cloud.google.com/compute/docs/instances/limit-vm-runtime).
