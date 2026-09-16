# Create alternatives worth selecting

A better selector cannot help when all repair candidates produce the same result.
Before comparing WAM or experience-based selection, MissionOS should test whether
its candidate set contains useful alternatives from the same failure state.

Candidates should change different aspects of the action: lift higher, take a
different corridor, or place and grasp again from the opposite side. An LLM may
propose a repair family. Deterministic code translates that proposal into bounded
operations, and feasibility checks remove unsupported or unsafe candidates.
Human approval, execution and verification remain separate steps.

Evaluate every admitted candidate from the same restored start. Count safe
successes and failures, preservation violations, and completion steps. Compare
the best possible choice per start with both a fixed baseline and the best single
family across all starts. This distinguishes a useful selection problem from
one consistently better repair program.

The implementation provides candidate templates, exhaustive evaluation and a
bounded closed-loop primitive adapter. The adapter requires a robotics session
that supplies real control, observations and verification. A measured frozen
50–100-start evaluation must establish useful outcome differences and improvement
room before investigating selectors on these candidates.

The [maintainer contract](../agents/causal-repair-candidates.md) defines the
primitive semantics, measurements, rejection accounting and admission gate.
