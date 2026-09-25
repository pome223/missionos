# Mission Assurance

Recovery proposes how to respond to a problem. Mission Assurance judges whether
that response fits the mission as a whole. The same MissionAssuranceAgent is used
for PX4 and TurtleBot3; their adapters supply observations and available actions.

For example, Recovery can propose going around an obstacle. Rules check whether
the candidate path is feasible. Mission Assurance can recommend proceeding with
that change, holding, or asking the operator for guidance. Its judgment does not
grant permission to move.

A navigation world model can supply forecasts for the current recovery candidate
and holding. Mission Assurance can use those forecasts through the configured
judge, including Jev. Prediction describes a possible future; the Verifier still
checks what actually happened. An optional comparison mode records forecasts
without changing the judgment. When predictions are required, unavailable or stale
forecasts stop the decision for review. A compatible navigation model must be
supplied separately from the stacking model.

```text
Observe -> Recovery proposal -> feasibility -> Assurance judgment
-> human approval -> fresh feasibility -> execution -> verification -> observe again
```

Approval binds a particular proposal. If the situation has changed, the approved
action can still be refused. The system does not silently substitute a different
proposal after approval.

The current adapters support opt-in simulator verification. A command being
accepted, a robot moving, an obstacle being cleared, a route finishing, and a
delivery being completed are separate observations. None proves physical-world
execution by itself.
