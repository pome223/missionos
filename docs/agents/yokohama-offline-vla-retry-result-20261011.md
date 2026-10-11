# Single approved native image-pair retry

The 22:28 UTC user approval allowed one further create command within the same
USD 1 comparison group and cumulative USD 8 authorization. It did not restart
hourly execution or reset spent amounts. The request at **22:49:09.442 UTC**
failed with **Google Cloud HTTP 503: Internal error**. No VM, disk, address or
matching insert operation was observed. Warmup, open/blocked comparisons, WAM
and flight counts are all **zero**; no request was resent.

Independent REST observation confirmed target absence at **23:03:43.355 UTC**,
inside the 900-second window ending **23:04:09.442 UTC**. After that window,
**23:05:32.610 UTC** SDK/REST checks confirmed zero VM/disk/address resources and
no matching operation. The controller, REST watchdog and no-ack guard were all
stopped. No unbound deletion was attempted and no insert DONE state was invented.
The missing operation acknowledgement remains part of the failed result.

The prepared public source is `de6ad9f`, byte-identical to the prior 141-case CPU
qualification for its eight bootstrap/guest files. Three current host-controller
cases checked startup syntax, independent watchdog behavior and waiting for an
unassigned NAT address. Private controller changes were the new approval time,
NAT readiness and immediate create-response reporting; generation and deadline
limits remained unchanged. This is not new GPU/model qualification.

Current pricing retained the USD 0.409901396 resource estimate, or
USD 0.4918816752 including a separate 20% tax/FX contingency, within the
USD 0.5431080457 remainder. No allocated usage was observed in this attempt;
provider invoices remain unconfirmed. Prior estimated group spending stays
USD 0.4568919543 and cumulative spending USD 1.7512037928; reservations stay
USD 7 under USD 8. No unused monetary balance authorizes another attempt.

The original failures, approvals, ledgers and stop marker are retained, and the
hourly task remains disabled. Starship was not changed. Raw logs, source, rules,
price snapshots and final checks are in a small restricted local archive. Its
SHA-256, size and stream-restoration record are in the companion JSON. Prior
large archives were not rewritten. Later PR publication has a separate explicit
user approval and does not authorize more GPU tests, merge or deployment.
