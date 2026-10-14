# Tower or ocean: a recorded recovery judgment

MissionOS passes the current catch-development report and the booster observation
to DeepSeek inside the mission. DeepSeek selected splashdown; the independent
execution check accepted it and a later observation confirmed the selection.
The same flight reached its modeled water-entry limits.

[![Recorded DeepSeek recovery judgment](../assets/starship-booster-recovery-decision-20261014/mission-poster.jpg)](https://github.com/user-attachments/assets/00a07f6d-93a3-407b-9f38-b6d017bd1c72)

[Play the 120-second video](https://github.com/user-attachments/assets/00a07f6d-93a3-407b-9f38-b6d017bd1c72)
· [Interactive replay](../assets/starship-booster-recovery-decision-20261014/index.html)

## Follow the decision

All times below belong to the saved flight shown in the video. Randomized fault
experiments are separate executions.

| Mission time | Observation, decision or outcome |
| --- | --- |
| T+135.6 s | The host supplies current Booster observations and the reviewed report of zero launch-derived supported catches. |
| T+137.0 s | DeepSeek selects splashdown; the independent execution check accepts the action without fallback. |
| T+137.1 s | A distinct later observation confirms the recovery objective. |
| T+619.655 s | Booster reaches nominal sea contact: CG speed 1.847 m/s and propellant 11.42 t. |

The report informs judgment; it does not create execution authority. Splashdown
is the only executable recovery objective in this record. Payload release is a
separate Ship sequence, so the 26 releases are not attributed to the Booster choice.

The tower is visible at launch and in a labeled option cutaway. Capture has zero
demonstrated launch-derived supports and its controller remains unconnected;
only ocean return is currently executable. No tower approach or catch is depicted.
Payloads are generic rigid bodies: unfolding, communications and orbit raising
are not modeled.

| Recorded fact | Result |
| --- | --- |
| DeepSeek booster choice | Splashdown, accepted without fallback |
| Booster CG speed at sea contact | 1.847 m/s, nominal separation only |
| Payloads released | 26/26 |
| Ship contact-point speed | 4.608 m/s, low-speed contact |
| Separate Ship orbital-comparison gate | Incomplete after a model-summary rejection |

The video shows the model's external explanation and actual telemetry. Hidden
reasoning is excluded; the explanation's prose is not independently verified.
The first rejected booster answer and a separate API diagnosis remain recorded.
This demonstrates the requested recovery-judgment path, rather than a newly
completed M3 campaign, robust recovery or real spacecraft operation.

## Engineering value and prior work

The implementation connects time-bounded live model judgments, once-approved
mission scope, a credential-free 6DOF worker, execution checks and later
observations. Its reusable pattern is to delegate mission decisions, constrain
execution independently and record what actually happened. The synchronized
video makes that sequence visible, including rejected answers and incomplete gates.

Autonomous spacecraft planning, execution and fault management have prior
examples, including NASA/JPL's 1999
[Remote Agent experiment](https://www.jpl.nasa.gov/news/remote-agent-experiment-meets-all-objectives/).
Independent monitoring and fallback also have established research in
[NASA's Runtime Assurance framework](https://ntrs.nasa.gov/citations/20240006522).
The contribution claimed here is an implemented, inspectable integration for
LLM mission supervision; research originality requires a broader related-work
comparison. Neither those precedents nor passing simulation checks establishes
real-time certification or safety of this implementation on hardware.

The [randomized fault-notification trial](starship-trouble-engine.md) demonstrates
actual AI responses to supplied idealized health flags. Independent symptom
diagnosis and corrective recovery remain future work. Rules and fallback parity
are regression and safety-net evidence, not an AI-value score. Assessment asks
whether AI performed the delegated control work using the information available
at the time, with separate records of judgment, authority, execution and outcome.

[Reviewed record and limitations](../assets/starship-booster-recovery-decision-20261014/summary.json)
· [Authority and verification contract](../agents/starship-booster-recovery-history.md)
