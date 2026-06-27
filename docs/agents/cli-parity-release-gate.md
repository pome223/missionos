# CLI Parity Release Gate

This repository remains private until the extracted MissionOS CLI is verified to
behave like the upstream private CLI for the supported MissionOS operator
workflow.

The goal is not byte-for-byte source equality. The goal is operator-visible
behavioral parity against the same Gateway and runtime boundaries, while removing
private package names, private paths, and publication-unsafe assumptions.

## Release Condition

The repository may be made public only after all required CLI parity checks pass
and the evidence is recorded in the release PR or release checklist.

Required checks:

- command inventory parity
- help text and option parity for public-supported commands
- fixture Gateway parity
- real loopback Gateway parity
- scoped runtime smoke for any runtime path claimed in public docs
- publication safety scan

## Command Inventory

Generate the command inventory from both repositories during verification rather
than trusting this document as the source of truth.

The expected operator surface includes:

- `missionos gateway start`
- `missionos gateway status`
- `missionos gateway stop`
- `missionos gateway restart`
- `missionos status`
- `missionos say`
- `missionos approve`
- `missionos reject`
- `missionos revision`
- `missionos run`
- `missionos repair`
- `missionos clear-state`
- `missionos recover`
- `missionos start-sitl`
- `missionos execute-sitl`
- `missionos job-status`
- `missionos watch`
- `missionos map`
- `missionos operate`
- `missionos rtl`
- `missionos land`
- `missionos tutorial`
- `missionos chat`

If a command is intentionally withheld from the public CLI, document the reason
and ensure human-facing docs do not claim it exists.

## Parity Levels

### 1. Static CLI Parity

Compare command inventory, help text, option names, defaults, and exit behavior.

Evidence should include the exact commands used to capture help output from:

- the upstream private CLI
- extracted `missionos`

### 2. Fixture Gateway Parity

Run both CLIs against the same deterministic fixture Gateway responses. This
must cover at least:

- health/status
- conversation request payload for `say`
- intent commands such as `approve`, `run`, and `repair`
- `job-status` rendering from a saved task
- `watch` rendering from fixture runtime state
- `map --snapshot --no-open` artifact generation from fixture route data

### 3. Real Loopback Gateway Parity

Start a real Gateway on a loopback port and call it from the extracted CLI. The
Gateway may be the current private Gateway during migration, but the command,
port, state path, and observed result must be recorded.

Use an isolated port and state path so parity runs do not pollute an existing
operator session.

### 4. Runtime Smoke

If public docs claim SITL or runtime behavior, run the scoped production boundary
that supports that claim. A suitable sequence is:

```bash
missionos say ...
missionos approve
missionos run
missionos start-sitl --task-id ...
missionos execute-sitl --task-id ...
missionos job-status --task-id ...
missionos map --task-id ... --snapshot --no-open
```

The observed result must keep proposal, approval, dispatch, ACK, runtime
progress, landing, delivery completion, and physical execution separate.

### 5. Publication Safety Scan

Before switching the GitHub repository visibility to public, scan the outgoing
tree for:

- credentials, tokens, cookies, and API keys
- private task databases
- private generated output
- local absolute paths
- private hostnames or endpoints
- stale upstream package references that would break the extracted CLI

## Non-Negotiable Claim Boundary

Do not publish with docs or CLI output that implies:

- AI proposal means approval
- approval means dispatch
- dispatch means ACK
- ACK means runtime progress
- runtime progress means landing
- landing means delivery completion
- simulator execution means physical execution

If parity evidence is partial, keep the repository private or publish only the
parts whose runtime boundary has been verified.
