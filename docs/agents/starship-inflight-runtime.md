# Inflight approval to a return reservation

This adapter is opt-in and covers a single request to add `select_next_orbit`
to a previously approved scope containing nominal return and
`keep_reserved_return`. The registry may already contain next-orbit return,
but the active scope does not. Resource limits and the existing return horizon
(including the 6000-second cap) do not expand. The original simulation and wall
decision deadlines bound the request and cannot be restarted.

## Trust and process boundary

The host owns `InflightApprovalLedger`, its HMAC key and the operator bearer
token. It creates the request, serves the operator UI and consumes the response.
The worker does not receive either secret or a ledger handle. The host sends the
existing signed consumption record over an anonymous inherited pipe. The worker
checks context, request digest, original deadline, revision, unchanged resource
limits and the expected command addition. It does not possess the HMAC key and
does not claim cryptographic verification of that signature. Record integrity
is checked later against the host-owned signed ledger. Host authentication is
provided at runtime by the inherited channel, not by a model's JSON output.

Create two `approval_pipe()` pairs in the host. Pass only the request write FD
and grant read FD to the worker through the subprocess `pass_fds` allowlist;
close their unused host copies after spawning. Keep the request read FD and
grant write FD exclusively on the host. Do not use inherited-all-FDs spawning,
named files, model tool output or an untrusted callback as the grant channel.
The adapters require FIFO read/write endpoints, use bounded JSON frames and
nonblocking I/O. This protects against model-originated messages, not a fully
compromised same-user host or modified executor code.

## Runtime API

`HostApprovalBridge(ledger, context, snapshot, request_reader, grant_writer,
bounds, event, clock)` accepts keyword arguments. `context` binds run, mission
and vehicle. The trusted snapshot contains `run_active`, `simulation_time_s`,
`revision`, `scope`, `observation`, `reservation_sha256`. It may also supply `known_observations`, at most 64 observation records captured by the trusted host snapshot source. The worker request observation must exactly match the current observation or one of those records; its original timestamps must still meet the freshness limit. Do not populate that history from the request itself. `bounds` contains the
fixed `simulation_deadline_s`, `wall_deadline_s`, `maximum_observation_age_s`.
Host `tick()` accepts at most one worker request and later consumes a human
response or expiry; call it repeatedly, never in a blocking wait. Register
`host.snapshot` with the inflight Gateway service; it projects the six fields
that route expects, including the pending request ID.

`WorkerApprovalActor(context, scope, revision, request_writer, grant_reader,
bounds, admission, command, event, clock)` has no host secret. Call
`request_change(request_id, candidate_sha256, state)` once, then call
`tick(state)` between actual physics steps. The actor neither advances physics
nor sleeps. Both processes use the same host monotonic clock. The worker can
expire the request by itself if the host is unavailable.

`admission(action, current_state)` independently checks the still-current
candidate, returning `accepted` and, for next-orbit return, `candidate_sha256`.
Even an approved grant is unused if the candidate has changed or is no longer
admitted. `command(action, current_state, consumption)` changes only the return
reservation; it returns `applied`, `revision`, `reservation_sha256`. The callback
must update the authoritative scope/revision together with that reservation.
Actual burn dispatch must still run the existing executor's fresh physical
checks. An uncertain callback result is recorded as unknown and never retried.

After a change, a strictly later observation must match the new reservation,
revision and scope. The event is `m3_return_reservation_observed`, not physical
return confirmation. Rejection, timeout, stale context or invalidated candidate
leaves the current reservation unchanged, rechecks only that existing reservation
and records `m3_scope_update_not_applied`. It does not search for a new candidate,
change a reservation on timeout, authorize a burn or guarantee a safe return.
If the current reservation no longer passes its check, the adapter reports
`fallback_not_admitted`; the existing dispatch gate remains responsible.
Persist both `host.record()` and `actor.record()`: they retain the original
request frame, signed host result, independent-check event and subsequent
reservation observation without either host secret. Neither record claims the
worker verified the HMAC, a burn happened or a physical return succeeded.

## Verification boundary

Tests use real anonymous pipes and host ledger with fake current-state,
admission and reservation callbacks. They cover advancing observations while
pending, approval, rejection, both deadline clocks, host absence, changed
revision, invalidated forecast, duplicate/replayed request and later reservation
confirmation. They do not establish a live model call, HTTP human response,
continuous 6DOF, a burn dispatch or mission success. The Gateway HTTP smoke and
actual executor integration must be reported separately.

## Candidate binding and legacy scope requests

The M3 bridge requires `candidate_sha256`. The host signs it into the request, operator receipt and consumption. The operator page displays it and echoes it on approve/reject; the worker requires the same value in its original request, signed records and fresh admission result. A consumed approval can still be unused when the candidate or current state fails admission. This binds the immediate reservation operation; it does not claim the worker verified the HMAC or that a burn occurred.

The ledger API accepts `candidate_sha256=None` for existing scope-only callers, preserving their signed record shapes. Such requests are displayed explicitly as a scope-only amendment, not a specific-candidate approval; M3 cannot consume them. No resource cap or original deadline expands. Callback failures before the reservation command trigger a fresh check of the existing reservation. After a command has started, any error or malformed acknowledgement is recorded as `reservation_update_unconfirmed`, never retried or reported as unchanged.

The optional `bounds.reservation_cutoff_s` is the trusted caller's already-admitted burn time minus its reserved decision margin. When supplied, both bridge and actor require the original simulation approval deadline not to exceed this cutoff. The integrated M3 caller must supply it for a valid existing booking; this component does not discover a burn time or widen its budget. Active-scope enforcement in the actual executor is still a separate integration requirement; adding a command to an example scope does not enforce it across the full mission.

A consumed human approval records `approval_disposition=approved_pending_executor`, `scope_applied=false`, and `revision_semantics=authorized_revision_not_applied_revision`. The consumption's revision is the authorized candidate revision. Only the worker's actual reservation update and its later observation show that the revision was applied. An invalid worker request produces a fixed-reason host rejection and consumes the one-attempt bridge budget; it cannot stop physics or restart the original approval deadline.
