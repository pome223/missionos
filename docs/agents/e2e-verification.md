# E2E And Runtime Verification

Runtime changes require runtime verification. Unit tests are useful, but they do
not replace a smoke test against the affected production boundary.

## Minimum Examples

- CLI: run the command entrypoint with a fixture or real loopback Gateway.
- Gateway: start the server on `127.0.0.1` and call the route with a real HTTP
  client.
- Map: generate the HTML artifact from fixture task state and inspect the output
  path.
- Runtime adapter: run the fixture adapter or an explicitly opt-in SITL smoke.

## PR Evidence Format

Every PR with runtime impact should include:

- exact command
- scenario exercised
- production boundary covered
- observed result
- key task ids, fixture ids, status codes, timeline events, or artifact paths
- skipped parts and environment limitations
