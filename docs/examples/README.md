# MissionOS Examples

Examples should read like short operational stories. They should show the
mission, the operator action, the evidence collected, and what remained
unproven.

## Examples

Each example should include commands, observed evidence, and limitations.

- [MissionOS Chat: Tokyo Station to Akihabara](missionos-chat-tokyo-akihabara.md)
  shows an actual LLM-backed `missionos chat` planning run. It stops at proposal
  and does not claim approval, dispatch, ACK, progress, completion, or physical
  execution.
- [MissionOS Chat: Obstacle Recovery Run](missionos-chat-obstacle-recovery.md)
  shows an actual obstacle-context `missionos chat --autostart
  --enable-live-sitl` run. It includes human-approved recovery dispatches,
  `watch`, `operate`, a map screenshot, and the terminal limitations.

## Example Checklist

Each example should state:

- scenario
- exact commands
- production boundary exercised
- observed task id or conversation route
- observed evidence
- warnings and limitations
- whether delivery completion and physical execution were proven
