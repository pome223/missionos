# Authorized cold AeroVLA image-pair attempt

The single authorized October 11 cold-start attempt failed because the terminal
cloud insert operation reported `ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS`: one
L4 with `g2-standard-16` was unavailable in `us-west1-a`. The local SDK command had
first exceeded its 30-second response limit after a successful zone lookup; its
log did not record the insert POST. Subsequent reconciliation identified the
terminal failed insert and established the definitive cloud error. The operation
ended at 04:43:37.655 UTC. No VM or disk was observed through the original
600-second deadline. No second create call was issued.

The regional public-price review qualified `us-west1-a`, `g2-standard-16`, one
L4, 200GiB Balanced disk and one ephemeral external IPv4. At a ten-minute runtime
and at most 1GiB outbound traffic, the conservative estimate is USD 0.426601
before tax and currency conversion. The attempt reserve remains USD 1, with
USD 6 of prior conservative reserves and an aggregate USD 8 authorization.
This is not a provider invoice or a provider-enforced spend cap.

The approved cold-download conditions allocated 360 seconds for preparation,
90 for comparison and 150 for collection/deletion. The prepared guest would
install packet quotas before downloading, stop at conservative 31GiB received
or 896MiB sent thresholds, and supervise absolute stage deadlines. These guest
controls were not installed because no VM was observed. The create request
contained an earlier absolute DELETE termination time and boot-disk auto-delete;
those settings were not verified on a provisioned resource.

`scripts/yokohama_offline_vla_remote.py` uses the existing hash-checked native
model loader, pinned OpenVLA/AeroVLA revisions and the qualified Torch/L4 runtime.
It performs at most one synthetic warmup and two comparisons, with no retries,
generation-time action mask, WAM or vehicle access. Both saved cases use the same
neutral prompt and generation settings; evaluation labels and geometry remain
outside the model input. Raw responses and token IDs would be retained unchanged.

No weight download, warmup, comparison inference, WAM call or flight was observed.
There are no native raw responses or image-pair interpretation results. The
87 passing CPU checks and lint validate code boundaries, including identical
inputs and stopping on a callback error; they do not establish model inference,
obstacle avoidance, delivery performance or the operational response lease.
An initial broader CPU run lacked the repository package source path; its
86-pass/1-import-failure log is retained alongside the corrected 87-pass run.

See `yokohama-offline-vla-cold-attempt-result.json` for the complete small result
record. The earlier cache-gate abort record is retained as a separate phase.
Code, exact saved input images, approval, regional price source pages, command
records, terminal cloud failure, initial SDK diagnostic and delayed-create reconciliation are preserved
locally. No push, PR, merge, deployment or new persistent access was performed.

Pricing sources: [GPU](https://cloud.google.com/products/compute/pricing/accelerator-optimized),
[storage](https://cloud.google.com/compute/disks-image-pricing),
[network](https://cloud.google.com/vpc/network-pricing).
