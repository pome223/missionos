# Local simulation inflight scope amendment

`starship_inflight_approval.py` is an opt-in authority ledger. It does not
dispatch a command, integrate physics, contact a model, deliver a notification,
or prove an observed outcome. Gateway delivery and executor integration must be
verified separately before claiming an inflight human control loop.

The host configures a named local operator, a bearer token, an HMAC key and a
fixed registry. Keep the token on the host/operator side; never include it in a
model prompt, simulation worker environment, request artifact or report. A worker
uses `operator_token=None`, which cannot resolve operator requests. Token
possession authenticates this configured local principal, not a person through
SSO. Neither client-supplied names nor AI decisions create operator authority.
HTTP handlers authenticate status/notification requests with
`authenticate_operator(token)` before returning ledger data; `resolve` repeats
authentication. `status` is an internal accessor, not an unauthenticated route.

Scopes are `{commands: [registered_id], resource_limits: {resource_id: maximum}}`.
Resource identifiers have fixed units and semantics in the caller's contract.
The registry defines the outer command set and resource ceilings. A delta is
`{add_commands: [...], resource_limits: {existing_id: new_absolute_maximum}}`.
It can only add registered commands or increase existing limits within those
ceilings. It cannot supply code, change units, add unknown resources, waive
physical checks or authorize hardware. The fallback must already be in the
current scope.

Create a request bound to mission, vehicle, current revision, scope hash, delta,
observed-state hash and original simulation/wall deadlines. A candidate-bound
request also signs `candidate_sha256`. Display these bindings to the operator:
`resolve` requires the displayed request and observation hashes, plus the same
candidate hash when present, the bearer token and an approve/reject choice.
M3 requires candidate binding; legacy scope-only callers retain the two-hash
form and cannot authorize a specific M3 candidate. The caller must obtain
the simulation time from its live worker, not the HTTP request body. The ledger
obtains the wall time itself. Deadlines cannot be extended; either clock reaching
its deadline expires authority, including approval received earlier.

`consume` is a single SQLite transaction. Supply the actual current simulation
time, live mission flag, revision, scope and fresh observation. It rejects a
changed context, clock regression, stale/future observation, forged receipt or
second consumption. A fresh observation may have a different state hash from
the request: it is separately recorded and must be physically checked by the
executor. An approved consumption returns revision + 1 and the expanded scope;
rejection/expiry returns the unchanged scope and original fallback. Pending
requests cannot be consumed before expiry. Failed preconditions return no grant.

All participating processes use one host's monotonic clock and one mission's
simulation clock. A host reboot invalidates outstanding requests; callers must
not resume them across boots. Use a run-owned private directory. The executor
must recheck current physical feasibility and resource availability immediately
before any command, apply its own bounded fallback on ledger/observation failure,
and record a later observation independently. A signed grant is approval only.

Unit tests cover bounds, replay, context and clock changes, expiry, malformed
authentication and record corruption. They do not establish Web delivery, human
response, model judgment, a physical action, successful return or safe fallback.
