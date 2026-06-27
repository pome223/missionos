# Agent Instructions

These instructions apply to automated coding agents working in this repository.

## Documentation Layers

Keep documentation in two layers:

- `docs/concepts/` and `docs/examples/` are for humans. Prefer short, abstract,
  readable explanations.
- `docs/agents/` is for AI agents and maintainers. Prefer explicit contracts,
  field semantics, verification requirements, and publication rules.

Do not move implementation details into the human layer unless they are required
to understand the concept.

## Claim Boundaries

Preserve the MissionOS authority split:

```text
LLM judges.
Human approves.
Rules constrain.
Executor acts.
Verifier checks.
Repair loops.
```

Do not describe an AI proposal as approval, dispatch, execution, landing,
delivery completion, or physical execution. Keep these as separate facts.

## Public Safety

This repository is private while it is being prepared for publication. Treat all
incoming code as a publication event:

- do not import private task databases, private generated output, credentials,
  local secrets, or unreviewed evidence artifacts
- do not add local-only paths from another workstation or private checkout
- keep hardware and live simulator execution opt-in
- prefer fixtures for public demos and tests

## Pull Request Verification

Before opening or updating a pull request, verify the change with a runtime smoke
test that exercises the affected production boundary. Unit tests alone are not
sufficient for PR readiness when runtime behavior changes.

Examples:

- CLI changes: run the CLI entrypoint with a minimal real invocation.
- Gateway changes: start the Gateway on a loopback port and call the route with
  a real HTTP or WebSocket client.
- Simulator adapter changes: run the fixture adapter or opt-in SITL smoke that
  covers the changed boundary.
- Documentation-only changes: run link or formatting checks when available.

Every PR body should include an `E2E / Runtime Verification` section with the
exact command, scenario, boundary covered, observed result, and limitations.
