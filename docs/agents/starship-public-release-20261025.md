# Starship selected public port — 2026-10-25

## Scope and provenance

The human explicitly authorized creating and merging a public PR. The release
starts from public PR 132 (`bdf95820`), itself based on public main, and selects
Starship source, tests, fixed configuration and compact public-data fixtures from
internal `5d23fa48`. It does not merge internal develop/history or import private
output directories, task stores, credentials, environments or raw flight/model
transcripts. Existing public PR 132 media and its dated evidence stay unchanged.
Unrelated CLI/Gateway/core changes from internal develop are excluded.

The selected source inventory is recorded in
`starship-public-release-manifest-20261025.json`. Hashes are provenance, not runtime
admission. The public file set is a new source inventory. Historical run labels,
qualification identities and failed records are never rewritten to claim the
public checkout flew or acquired permission. The public documentation reports
exact-point qualification separately from the unexecuted adapter verification.

## Publication review

Selected text was scanned for personal workstation paths, private-key blocks,
common provider/access-token values and private repository references. Generic
`/Users/private/...` strings in negative protocol tests deliberately exercise
path-leak rejection; they are not workstation paths. Two historical registration
fixtures name the private source repository as provenance metadata; they contain
no credentials, private endpoint, or copied private output. Secret-manager names
are configuration identifiers, not secret values. Numerical public-display data,
synthetic contract fixtures and compact reviewed aggregate results are the only
new evidence assets. No full private qualification certificate is published or
installed as a normal permission root.

## Evidence and authority boundaries

The adapter is an explicit entry for a host that already owns the sealed actual
handoff and normal permission callback. It is not globally installed over the
legacy launch paths. It checks current source/profile/configuration, held objects,
inventory and every actuator-history row before calling existing permission code.
Normal entry is restricted to exact nominal continuation; fault-cell future
numerical applications are not assumed at the pre-fault permission epoch.

Old 39-corridor readiness and source mismatch checks remain fail-closed. A public
port is not a qualification refresh. Live flights, new provider calls, retries,
threshold relaxation, hardware execution and tower catch are not part of release
verification. The prior capacity stop stays a stop.

## E2E / Runtime Verification

Commands and observed public-checkout results are appended after execution.
Contract suites, real HTTP/inherited-pipe fixed-response smoke, source/privacy
checks and local documentation links are reported separately from historical
flight outcomes. Hosted CI results must refer to the exact public PR head.

### Observed public-checkout boundary checks

- `python scripts/smoke_starship_final_runtime.py --run --output output/starship-public-release-audit-20261025-v1/final-runtime-smoke`: **passed**, coverage revision 3; real loopback Gateway, nine inherited channels, fixed agent decoders and two visual HTTP requests. Physics steps=0 and provider calls=0. This is a fixture boundary receipt, not a flight.
- `python scripts/check_starship_current_admission.py smoke --output output/starship-public-release-audit-20261025-v1/current-admission-smoke.json`: **passed**; synthetic held-owner/certificate facts, one existing-host permission callback, saved JSON and duplicate entry refused. No actual human approval or operational dispatch.
- `ruff check src/gateway/starship_chat.py`: **passed**.
- Selected-file publication scan: 459 files, no actual personal workstation path/private-key block/common token value found. Local links in the README and new documentation: **87 checked**, no missing target. This is not a comprehensive security audit.
- The initial broad contract run exposed an inherited test expecting the old certificate to remain valid after source changes. The public adaptation now requires source-mismatch refusal; its six-test file passed. No runtime admission condition, certificate, flight threshold or historical record was weakened. Broader matrix results are recorded separately at the final PR head.
