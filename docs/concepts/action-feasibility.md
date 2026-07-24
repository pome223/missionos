# Action feasibility

MissionOS separates choosing an action from proving that it is safe enough to
offer for approval.

The shared Core records which facts were observed, which values were derived,
how fresh the evidence is, and which policy and model versions were used.
Robot-specific adapters calculate details such as clearance and performance.
The result is one of:

- verified feasible
- blocked by evidence
- unverified because evidence is missing or cannot be compared

An AI may compare and explain these results. It cannot turn a blocked or
unverified action into an executable one. A feasible result is still only
evidence: human approval, dispatch, execution, observed progress, and mission
completion remain separate facts.
