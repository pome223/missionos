# missionos-cli

Operator-facing command line package.

The primary public path is `missionos chat --autostart` with an explicit LLM
backend. MissionOS is designed for the LLM to judge, plan, diagnose, and propose
bounded mission actions while the human operator approves and Gateway/rules keep
dispatch, runtime evidence, and completion claims separate.

The CLI also carries Gateway commands, local mock/fixture boundary-test paths,
experimental `play` / `tutorial` surfaces, and opt-in SITL helpers. Treat those
as development or experimental surfaces unless you verify them in your own
environment.

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

Use `missionos chat --autostart` with `MISSIONOS_LLM_BACKEND=gemini` or
`MISSIONOS_LLM_BACKEND=ollama` for the intended MissionOS chat path.
