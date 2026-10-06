# Deployment-status collection: bounded operational slice

This adds one read-request / measured-report / reassessment path to the existing
six-DOF missing-separation experiment. It does not diagnose a SpaceX mechanism,
retry an actuator, change guidance, or establish LLM/Jev mission benefit.

## Primary operational basis

- [ECSS-E-ST-70-41C, 15 April 2016, section 6.3.3.7](https://ecss.nl/wp-content/uploads/2016/06/ECSS-E-ST-70-41C15April2016.pdf)
  defines a one-shot housekeeping report request, TC[3,27], and a resulting
  parameter report, TM[3,25]. This motivates an explicit report request followed
  by report reception. The local adapter does not encode PUS packets or claim
  compliance; the cited edition is the source, not a claim about the latest revision.
- [ESA: Solving the RIME deployment mystery](https://www.esa.int/Science_Exploration/Space_Science/Juice/Solving_the_RIME_deployment_mystery)
  describes engineers combining actuator activity, spacecraft motion and camera
  evidence to confirm deployment. It supports separating a command acknowledgment
  from its measured result, not importing RIME recovery commands into Starship.
- [Yamcs command verification](https://docs.yamcs.org/pymdb/verifiers/)
  distinguishes receipt, acceptance, execution and completion checks using later
  telemetry. This work follows that distinction but does not connect to Yamcs.

## Approved scenario and limits

`sixdof_observation_supervised` maps to the same `deployment_no_effect` physics
and fixed profile as the existing bounded supervision case. Its separately
displayed/signed procedure is `local_deployment_status_collection_v1`:

- at most one `deployment_status` read request;
- at most one actuator-facing operation: skip the remaining deployment sequence;
- at most two Jev assessments, and one conditional DeepSeek proposal, only in
  separately enabled live mode; fixture mode makes zero provider requests;
- 75 simulated seconds from the **first** decision request, with no reset after
  collection or reassessment; original return time and fuel reserve constraints;
- no deployment retry, restart, thrust, attitude or return-timing authority.

Ordinary supervision plans keep their existing one-assessment contract. A new
scenario grant binds the collection flag, source hashes and provider budget.
The worker checks the executed flag in study provenance. Provider credentials
stay in the Gateway; both calculation processes use credential-free environments.

## Execution and evidence

The original two observations create the first request. A bound `need_observation`
response must propose `hold`. At the next measured state, child-side Rules check
identity, orbital state, inhibited sequencer, no separation, fuel and deadline.
Only then is one collection request accepted. It does not skip deployment.

The same integrator continues during collection. A later observation, at least
two simulated seconds after issue, supplies the report. The operation receipt
binds request and report observation IDs, issue/reception times and the fixed
report kind. The fresh pair is sent for one reassessment. Any further observation
route holds; no third assessment or second collection request is generated.

The collected report is the next sample of the **same existing two-second
telemetry stream**, not a new sensor, camera, diagnostic channel or implemented
PUS request. Its measured state is later, but its parameter set is unchanged.
The direct fixture path skips at 529.7 s; collection and reassessment skip at
533.7 s, four seconds later. This is an operational transaction test with a
delay cost, not evidence that new information changed the decision or trajectory.

Reassessment is a new request with new evidence, not a retry of a failed provider
call. Transport failure, malformed/late replies, changed sources or child exit
stop the path. The broker does not retry. The collection interval and reassessment
remain paced while the continuous physical state advances.

The independent saved-record verifier links both read receipt observations to
physical samples, validates scope and clocks, then checks the sole skip against
another later observation. Two subsequent scheduled-interval observations are
required for a positive sequencer-effect result. A read ACK is not report reception;
report reception is not deployment; a skip ACK is not its observed effect.

The fixture broker deliberately asks for observation on the first assessment and
uses the existing fixture proposal on the second. That is protocol coverage, not
an inferred Jev decision or model-value comparison. Live routing is not forced.

## Outcome boundary

The existing automatic interlock already prevents later release. Therefore this
experiment cannot establish additional payload saving, lower impact speed or
improved mission success from collection. It tests the operational chain and its
failure behavior. The held mass, return trajectory and mission failures remain
visible. Choosing a different observation/problem and comparator would require a
new bounded study before an added-value claim.
