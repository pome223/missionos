# Publication Rules

Treat each migration into this repository as a publication event.

## Never Import

- credentials, tokens, cookies, or API keys
- private task databases
- private generated output directories
- unreviewed screenshots or evidence artifacts
- local-only absolute paths
- internal hostnames or private endpoints

## Review Carefully

- runtime code that can touch hardware
- simulator launchers and Docker invocations
- docs that describe safety, completion, landing, delivery, or physical
  execution
- examples derived from real user runs

## Preferred Public Shape

Publish only the public paths that have been verified in the current release
snapshot. Local mock/fixture paths may be documented as maintainer boundary
tests, but should not be presented as the main user demo unless they have been
validated and explained in plain language. Add live SITL or hardware paths only
after they are opt-in, documented, and fail closed when the environment is not
explicitly prepared.
