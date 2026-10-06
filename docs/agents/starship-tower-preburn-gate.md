# Preburn choice fixture contract

`starship_tower_preburn_gate.FixturePreburnGate` is an isolated, unconnected
Rules lifecycle fixture. It does not import a physical producer, activate a
catalogue entry, approve a plan, select an executable controller goal, issue a
command, or integrate a vehicle. Existing tower authority/runtime behavior is
unchanged.

Both supplied path assertions bind the same original plan, approval record,
source, profile,
catch configuration, site catalogue, actual-origin-state digest, complete
fault-history digest and carried origin-controller-context digest. Their common
preburn prefix must agree. Each assertion lists exact applicable checkpoint
state, simulation time, fault-prefix and carried-controller-context digests.
The fixture does not interpolate these points or infer a qualified envelope
around them.

Raw-record and independent-verifier-receipt hashes are caller assertions. This
module does not fetch, inspect or rerun those records. Even matching hashes and
`declared_terminal_objective_achieved=true` leave `qualification_verified`,
`physical_default_available` and `physical_activation_allowed` false. The
`fixture` mode is closed; a `live` or physical mode is rejected. No numerical
gate, timeout or boolean returned by this fixture admits an actual flight.

An untrusted caller-supplied assertion with the tower-directive schema must
match the same current state and
observation, source, plan, approval record, times and fixed run/request context.
The fixture checks only shape, self-hash and declared bindings. A caller can
repackage a model proposal in that shape; this module does not authenticate its
provenance, approval or origin in a supervision record. Every generated receipt
sets `directive_provenance_verified`, `supervision_record_bound`,
`actor_integrity_verified` and `approval_independently_verified` false, with
`directive_evidence_status=untrusted_caller_assertion`. Before a fixture choice
is consumed, the observation must remain
fresh, have exactly the current simulation time, and match both path assertions,
the phase must be
`recovery_boostback_slew`, and no site-specific command may have occurred.
The original deadlines cannot renew. At or after the latest preburn choice
boundary, a choice is rejected; the fixture does not retarget a body or assert
that a physical fallback exists.

A wall-clock fixture timeout or fresh negative tower readiness can request the declared
divert path only while the same preburn gate and both complete path assertions
still apply. Positive readiness is required for a capture directive. One
choice is consumed atomically; later choices cannot replace it. This fixture
default is distinct from an independently qualified, actively controlled
physical default diversion.

The simulation deadline cannot create a late default choice: the earlier or
equal latest preburn boundary closes first. If it is reached without an
applicable prior choice/default, the fixture records closure with no physically
available default.

Invalid assertion validation is transactional: an exception cannot advance
retained clocks or alter the receipt. A valid later `consider` tick still closes
the exact-state first-command reporting opportunity; that guard is intentional.

The external first-command assertion must use the exact state and time at
choice consumption, and the requested site. A separately supplied later-state
assertion must follow that command and advance simulation time. Both assertions
remain unverified. Goal application, measured physical effect, terminal contact
and outcome improvement require the future fixed physical producer and its
independent checker. A later mode/site flag is insufficient.

Before connecting this contract, the physical implementation must independently
qualify both site paths from the same actual fresh separation, validate the raw
records and verifier receipts, and establish the state/fault/controller-domain
of a default diversion throughout a continuously integrated pending interval.
It must commit before the first site-specific command and then verify later
physical effects without resetting the state or carried controller history.
The current fixture establishes none of those physical prerequisites.
Future activation must also bind the choice to an actual independently checked
`TowerSupervision` record and actor integrity. Matching a caller's self-hash is
insufficient. Local integrity material does not authenticate human identity.

The current stable `TowerWorkerContext.activate` and `read_activation` accept
only `recovery_boostback_burn` or `recovery_powered_entry_prepare`; their runtime
contract describes a post-planning early-return activation. This fixture's
`recovery_boostback_slew` phase is intentionally incompatible with that contract.
A real pre-first-burn activation requires an explicitly reviewed runtime-contract
and phase revision plus a fixed typed parent/driver connection. It cannot be
enabled by disguising a preburn state as an existing burn phase, or by activating
after the first site-specific command.

Validation: the focused 37 contract tests exercise source/origin/fault/tracker
binding, incomplete-path rejection, stale/late/wrong-phase checks, proposal
rejection, concurrent one-use consumption, exact-state command binding and the
separate later-state assertion. They perform zero vehicle integration and zero
provider calls. The new source and tests also pass Ruff.
