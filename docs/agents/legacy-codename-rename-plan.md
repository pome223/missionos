# Legacy Codename Rename Plan

This repository still contains a legacy internal codename in code identifiers,
runtime strings, service names, and simulator integration points. The remaining
legacy strings are not credentials or private evidence, but they should be
renamed deliberately so the public codebase presents one coherent MissionOS
identity.

Do not run a blind repository-wide replacement. Some strings are Docker service
names, gz-sim plugin identifiers, session namespaces, user-agent strings, UI
labels, or persisted defaults. Renaming them without a boundary-specific test
can break simulator startup, local state lookup, or compatibility with existing
fixtures.

## Current Inventory

As of the initial public cleanup, the public README and human-facing prose were
scrubbed, but code-facing references remained:

```bash
git grep -l "boiled-claw\\|boiled_claw" HEAD -- src scripts simulators | wc -l
git grep -n "boiled-claw\\|boiled_claw" HEAD -- src scripts simulators | wc -l
```

Observed inventory:

- 74 files
- 162 matching lines
- no tracked private databases, generated output trees, secrets, or local-only
  absolute paths were found by the publication scan

## Rename Principles

- Keep behavior stable first; rename only inside a tested boundary.
- Preserve the MissionOS authority split: LLM proposes, human approves, rules
  constrain, executor acts, verifier checks, repair loops.
- Do not rename a persisted namespace, Docker service, plugin registration name,
  or protocol-visible string without a compatibility note.
- Prefer `missionos` for public runtime names and `MissionOS` for UI/docs.
- Leave old aliases temporarily only when needed for compatibility, and document
  the removal condition next to the alias.

## Phases

### Phase 1: Public UI and CLI Strings

Scope:

- static HTML titles and labels
- CLI help text
- user-agent labels
- terminal banners
- docs-adjacent code comments

Verification:

```bash
git diff --check
missionos --help
missionos gateway restart --planning-only --wait
missionos say "Plan a fixture mission"
```

Expected result:

- no claim-boundary wording changes
- fixture planning still returns `progress_counted=False`

### Phase 2: Local Runtime Namespaces

Scope:

- default app names
- session namespaces
- memory namespace defaults
- audit/log names that are not fixture compatibility contracts

Verification:

```bash
pytest tests/contract/test_production_gateway_backend.py -q
missionos gateway restart --planning-only --wait
missionos status
```

Expected result:

- no migration reads private task databases
- newly created local state uses MissionOS names
- old local state is either ignored safely or handled by an explicit alias

### Phase 3: Docker, PX4, and Gazebo Integration Names

Scope:

- Docker service/container names
- gz-sim plugin names and namespaces
- SDF plugin references
- PX4/Gazebo smoke script defaults

Verification:

```bash
pytest tests/contract/test_production_gateway_backend.py -q
missionos gateway restart --enable-live-sitl --wait
```

If a change touches actual simulator launch or plugin registration, add the
smallest opt-in SITL smoke that exercises that changed name. Do not claim live
flight or delivery completion from a name-only smoke.

Expected result:

- fixture mode remains the default
- live SITL remains opt-in
- plugin and service lookup names resolve without fallback surprises

### Phase 4: Compatibility Cleanup

Scope:

- remove temporary aliases
- delete compatibility comments that no longer describe an active boundary
- update any tests that still assert the legacy name

Verification:

```bash
git grep -n "boiled-claw\\|boiled_claw" -- src scripts simulators docs README.md
pytest -q
missionos gateway restart --planning-only --wait
missionos say "Plan a fixture mission"
```

Exit criteria:

- no remaining legacy codename references in tracked public files
- no change collapses proposal, approval, dispatch, execution, verification, or
  completion into one success claim
- README and package quickstarts still run from a clean checkout
