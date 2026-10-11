# One-attempt native service regression controller

## Local prerequisites before cloud admission

The plan also pins `local_image_id` to the historical CPU qualification's
immutable Docker image ID, and binds `yokohama_local_preflight.py` by hash.
Before any cloud query or allocation, an owned CPU-only, network-isolated
container copies the same five world/model files used by the flight runner.
The host verifies their hashes and a nonce read/write handshake across the
actual trial filesystem. Docker metadata or a historical CPU pass alone is
insufficient: the container must start and complete this operation now.

The helper never pulls an image, requests a GPU, changes Docker configuration,
or restarts the daemon. It records an exclusive ownership nonce before create,
then uses exact name, label, image and bind source to reconcile/delete its own
container. A lost create response followed by an empty list remains unknown;
failed or unknown cleanup blocks admission. Preparation artifacts are retained.
The former anonymous model-copy container is replaced by this helper in the
flight runner too, so a startup timeout cannot silently abandon that container.

Cloud creation requires the same live process's successful, cleaned-up probe,
at most 30 monotonic seconds old after the cloud read-only checks and payload
snapshot. The Docker endpoint must be local Unix, and its daemon identity,
captured client environment, immutable image, parent path identity and copied
files must still match. A stale check fails closed; no old receipt is resumed.
Another local check occurs before flight after remote bootstrap. The exact
image ID travels in the approved flight arguments and final receipt checks.
The flight inherits the captured Docker environment, including absence of an
override; later environment changes cannot silently select another daemon.

These checks reduce the gap before allocation; they cannot guarantee Docker
will remain healthy afterward. A sibling-directory probe does not prove every
future path works. The actual flight performs the same owned preparation in
its fresh directory. Local failure after allocation triggers the existing
owned cleanup and never authorizes another paid attempt. Default description
remains offline; `--preflight` now explicitly includes this bounded local
container probe as well as read-only cloud checks.

## Explicitly authorized capacity retry groups

`scripts/yokohama_capacity_retry.py` adds a separately approved, single-use
group around the same single-attempt controller. Its default CLI is offline;
execution requires `--execute --operator-approval` with the exact group digest.
The group binds six unique child plans in advance, one new USD 5 funding grant,
and the same reviewed L4 configuration in `us-west1-a` / `us-west1-b` only.
Child plans use `budget_scope=bounded_capacity_retry`, share that budget ID and
USD 5 reservation, and require the live group authorization gate. Their
reservations are **not additive** and cannot be executed by the standalone CLI.
The estimated single allocated trial must still fit USD 2. The USD 5 aggregate
is an authorization and planning envelope, not an invoice-confirmed or enforced
provider spending cap. The guest-wide egress, deletion, tax and FX limitations
below also apply to the group.

Only a matching terminal insert Operation with a nonempty set of exclusively
allowlisted capacity errors, successful cleanup and a fresh empty VM/disk
inventory permits another attempt. Every successful inventory read is retained;
any observed VM/disk leaves a sticky record before deletion. Allocation, any
bootstrap/flight activity, mixed errors, interruption, changed source/approval,
or unknown create/cleanup ends the group. A lost create response is conservatively
nonretryable even if later cleanup resolves it. The first successful allocation
therefore permits at most one existing native flight, even if that flight fails.

Attempts are serial, with at least 600 monotonic seconds after the preceding
child has finished and its retry evidence has been checked. This is at least
10 minutes between submissions; preparation precedes the first attempt. Each
VM retains the existing absolute 3,600-second cloud DELETE deadline. Exclusive,
flushed ledgers are consumed before submission. There is no automatic restart
or resume of a consumed group, no overlapping attempts, and no reset of prior
funding or trial records. A process crash requires read-only reconciliation and
owned cleanup; it does not authorize resubmission.

The following single-attempt USD 2 contract remains the legacy default for
plans outside an explicitly approved USD 5 capacity group.

`scripts/yokohama_native_trial.py` describes a reviewed plan by default. It only
queries GCP with `--preflight`; mutations require `--execute` and a separate
operator approval matching the complete plan digest. The approval must cover
task-owned ephemeral cleanup and acknowledge that the communication allowance
is not a hard network byte limit and estimated costs are not a guaranteed spend
cap. No task plan or fixture is itself evidence of
human approval. No paid run has been established merely by adding this script.

## Proposed resource and cost envelope

One GCP `g2-standard-16` instance in exactly one approved zone, `us-west1-a` or
`us-west1-b`: one L4, 16 vCPU, 64 GiB RAM,
one 200 GiB `pd-balanced` auto-delete boot disk, ephemeral IPv4, no service
account or scopes. Version 2 plans reserve USD 2 against an incremental
USD 2 funding grant; each separately authorized plan permits at most one
resource-creation attempt. `budget_id` identifies the funding grant, while
`budget_scope=incremental_single_attempt` and `max_attempts=1` constrain the
current plan's admission. `aggregate_budget_usd=2` is the planning ceiling
across attempts charged to that grant, not a new grant for every plan.
An explicitly authorized later attempt under the same grant retains its
budget ID and records the prior consumed plan/attempt hashes, additional
funding of zero, and any unknown prior actual cost or unconfirmed balance.
`reserve_usd=2` is a planning allocation, not proof of USD 2 still available.
The controller does not reconcile a billing invoice or enforce cumulative
provider spending. Version 1's USD 2.50/USD 5 envelope is rejected;
old approvals and consumed attempt records must never be rewritten or reused.
Quota availability is not zone capacity. A capacity failure consumes the
attempt; there is no automatic paid retry or alternate-region fallback.
Changing even between the two allowed zones changes the plan digest and needs
its own authorization. A later authorized attempt uses a fresh directory and
resource name; the consumed attempt and its approval remain unchanged.

The revised 60-minute estimate, using rates checked on 2026-10-02, is USD
1.147208 for the instance, 0.027397 for disk and 0.005 for IPv4. A 1 GiB transfer
allowance at USD 0.23/GiB gives approximately USD 1.41 before tax/currency effects,
leaving about USD 0.59 within the USD 2 planning reserve. A new plan must refresh
the prices and contain a finite positive estimate no greater than USD 2.
These constants are
bookkeeping limits, not enforceable spend caps. This is an estimate, not a confirmed invoice or provider billing cap. A failed disk deletion
can continue incurring storage charges; guest-wide egress, taxes and account
currency conversion remain outside these controls. A strict guaranteed USD 2
ceiling is therefore not established. Execution requires explicit acknowledgment
of this limitation, or stays blocked if a guaranteed ceiling is required.
Account currency, taxes, exchange rates and the eventual invoice remain outside
these estimates. No credits or discounts are assumed. See the official [VM pricing](https://cloud.google.com/products/compute/pricing/accelerator-optimized),
[disk pricing](https://cloud.google.com/compute/disks-image-pricing) and
[network pricing](https://cloud.google.com/vpc/network-pricing).
The official [Deep Learning VM pricing](https://cloud.google.com/deep-learning-vm/pricing)
adds no separate image fee; underlying Compute Engine resources remain billable.

## Runtime and ownership controls

Before creation, a durable exclusive `attempt.json` records the plan digest,
budget ID, USD 2 reservation and
an absolute UTC deadline 3,600 seconds ahead. Creation requests
`--termination-time`, `--instance-termination-action DELETE`, auto-delete boot
disk and disabled automatic restart. An absolute deadline cannot be extended by
restarting the desktop controller. The controller verifies the returned cloud
scheduling and disk flags before bootstrap. GCP enforces this setting when the
desktop disconnects; this depends on the provider honoring its scheduling API.

Ownership labels are `managed-by=missionos-native-trial`,
`missionos-task=yokohama-service` and an attempt identifier derived from the
plan digest. Preflight refuses any existing matching instance or disk name.
Cleanup requires exact attempt labels, zone and, once observed, numeric instance
ID. It records an associated boot disk's numeric identity while attached to the
owned instance. Auto-delete is primary; a leftover disk is only explicitly
deleted if that exact identity is now unattached. No existing/unrelated resource
is adopted or deleted. Final inventory must show both instance and disk absent.

The controller waits at most 10 minutes for weights-only bootstrap, starts no
flight without at least 40 minutes remaining, and interrupts its flight before
the final five-minute cloud reserve. The existing runner performs model and
owned-container cleanup. Controller diagnostics and evidence failures do not
skip cloud cleanup. Independent flight verifiers run after cloud cleanup.
Actual SIGKILL/desktop loss cannot execute local `finally` blocks; the cloud-side
deadline is the independent backstop. Local Docker cleanup after such a loss
still needs inspection, and this is not a claim of crash-proof local cleanup.

Creation uses one asynchronous submission and retains the returned Operation.
After a CLI timeout or lost response, only read-only operation discovery is
allowed: exact instance target, project, zone, insert type and reservation time
must match. Cleanup must observe a matching terminal `DONE` operation before it
can treat inventory absence as final. Empty operation lists, pending operations,
query errors or ambiguous matches produce an unconfirmed cleanup receipt; they
never authorize another create. This deliberately does not claim cleanup success
when provider reconciliation is unavailable.

The controller revalidates the original approved plan immediately before create
and before flight. The compiled flight copies the approved source hashes; it
never approves freshly computed hashes after bootstrap. Derived endpoint/config
bytes are bound separately under the reviewed controller and original plan.

Child cleanup always attempts to kill the owned process group in `finally` and
reaps its Popen child with a bounded wait, including when the session leader has
already exited or exits on SIGINT. Real-process regressions cover an orphaned
SIGINT-ignoring descendant while preserving a separate sibling group.

## Communication allowance: precise limitation

The model client bounds each response at 24,000,000 bytes; evidence collection
refuses more than 32 MiB uncompressed or compressed. The archive sender also
reads at most 32 MiB, preventing an archive replacement/growth from causing an
unbounded download; its digest must still match. Those are application checks.
They do not cap SSH overhead, bootstrap traffic, TCP acknowledgments, arbitrary guest
traffic or total VM network egress. The 1 GiB figure in the estimate is therefore
an allowance, not an enforceable whole-VM quota. This controller does not change
firewalls, guest network policy or account billing configuration to claim one.
A requirement for a strict provider-wide byte/billing cap blocks this design
until a separately approved enforcement mechanism is supplied.

## Image and payload review

The prior known-used official image is
`deeplearning-platform-release/pytorch-2-9-cu129-ubuntu-2204-nvidia-580-v20260909`.
On 2026-10-02 GCP reported it READY but DEPRECATED, pointing to
`pytorch-2-9-cu129-ubuntu-2204-nvidia-580-v20261001`. The replacement is READY with
no deprecation state and the same named PyTorch 2.9, CUDA 12.9, NVIDIA 580,
Ubuntu 22.04 and Python 3.10 series. That is metadata compatibility, not tested
model compatibility. The exact selected image belongs in the approved plan;
the controller never silently substitutes it or retries after bootstrap failure.

The native payload requires an operator-supplied, reviewed, revision-pinned
AeroVLA/ANWM bundle. Its `bootstrap.sh`, `remote_lifecycle.py` and
`motion-adapter.pt` are external prerequisites and are not distributed in this
repository. The six model/helper Python files are tracked here. A fresh public
clone alone therefore cannot reproduce native execution. The plan records every
payload hash; Python model files must match this worktree.
Before any paid creation, the controller copies and rechecks the approved bytes
into a read-only task snapshot. Upload uses that snapshot and verifies remote
hashes before execution. Top-level Python package versions and model revisions
are pinned by the existing bootstrap; transitive packages are not a complete
lockfile. A runtime incompatibility is a failed trial, not permission to install
unknown alternatives. Existing SSH keys are required; host-key cache is kept in
the trial directory. No key generation, new account setup or model API key is
part of the trial.

## Acceptance and review boundary

A final plan binds the controller and lifecycle helper hashes, vehicle source
hashes, the five verifier entrypoint hashes, exact native payload, and the CPU run plus six passing verification
artifacts. Report the CPU run's actual source provenance if it predates a later
review fix; do not describe that as a run of the final revision.
The approved plan is revalidated before each post-flight verifier as well as
before create and flight. The entrypoint hashes do not lock every transitive
dependency or installed package. The new budget ID and reservation are reported
in both the offline description and the final controller result; neither is a
provider-enforced spending limit or permission to retry.

Native success needs the real city-model invocation evidence, payload receipt,
return/landing, all five existing independent verifiers, the vehicle-service
receipt, and resource cleanup. A passed controller does not measure incremental
AI value. Real model, cloud scheduling, remote shutdown and invoice behavior
remain unverified until an approved native attempt supplies evidence.
