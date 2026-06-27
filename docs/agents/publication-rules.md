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

Publish fixture-backed runnable paths first. Add live SITL or hardware paths only
after they are opt-in, documented, and fail closed when the environment is not
explicitly prepared.
