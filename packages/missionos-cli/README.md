# missionos-cli

Operator-facing command line package.

Initial migration target:

- fixture-backed `missionos gateway start/status/stop`
- fixture-backed `missionos status`
- fixture-backed `missionos say`
- fixture-backed `missionos job-status`
- fixture-backed `missionos start-sitl` / `execute-sitl` / `recover`
- fixture-backed `missionos map --snapshot --no-open`
- Gateway client extracted behind a clean interface

The CLI currently preserves the operator-visible command surface from the
research repository while its local Gateway launcher points at
`python -m missionos_gateway web`.

Operator prompts, help text, chat suggestions, and guided tutorial copy are
English by default. Gateway intent payloads are also English in this public
surface so local CLI behavior is language-consistent end to end.

## Local Install

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e packages/missionos-gateway -e packages/missionos-cli
missionos --help
```

Use `missionos chat --autostart` for a quick interactive session against the
current fixture Gateway.
