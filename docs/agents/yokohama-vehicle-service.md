# Yokohama vehicle execution service

The fixed harbour delivery chat path dispatches an approved mission to
`scripts/run_yokohama_vehicle_service.py`, a separate, single-use process. The
Gateway owns planning, human approval and independent completion verification.
The service owns the existing flight runner, DecisionHost, native VLA/WAM
adapters and lifecycle, and the network-isolated aircraft worker containing
CityDecisions. This is a local process boundary, not a physical onboard install
or a remotely authenticated vehicle protocol. PX4 remains the flight controller.

## Request and authority

The request is the existing `approved.json` containing a proposal and approval.
The service checks the approved proposal digest, fixed route and destination,
fixture/native backend, wait-only judge limits, exact argument catalog, and
approval-bound source/configuration hashes before importing the flight runner.
It accepts no arbitrary executable or extra simulator flags. The operator's
explicit `--approve-sitl` is still necessary for execution.

The standard Gateway request uses its bounded judge relay. An explicitly
approved offline trial can select the existing fixture judge with the same
catalog; this does not invoke a paid language-model API. Native city models and
the mission judge backend are separate choices.

The trusted boundary is the Gateway's local task store and protected job
directory. Hashes are provenance/binding checks, not signatures. This protocol
does not protect against an attacker able to replace executable sources or the
Gateway task store under the same OS identity. Native lifecycle configuration
is an explicitly approved local input, not executable text produced by a model.

## Status, replay and evidence

For a job directory containing `approved.json`, the only execution output is its
fresh `run/` directory. Atomic creation of `vehicle-service/` claims the job
once, including after a crash. A failed job needs a newly approved job; removing
the claim to replay it is not supported. The service saves the admitted bytes
to `vehicle-service/approved.json` and binds the runner to that snapshot.
For native execution, admission reads the service configuration once and checks
the digest of those exact bytes. It saves those admitted bytes as a read-only
`vehicle-service/native-service.json` and substitutes that snapshot path in the
runner arguments. Changing the caller's original file after admission cannot
change endpoints or lifecycle commands. The receipt and runner result both
record the configuration digest; completion checks it against approval.

`vehicle-service/status.json` transitions through `accepted`, `running` and
`finished`. It includes the service PID, proposal and manifest digests, backend,
exit code and final result digest. `finished` is not delivery completion.
The Gateway checks the receipt against its exact stored proposal **and**
approval, the run configuration and result, and still runs all five existing
flight verifiers. Native completion additionally needs both invocation flags;
the decision verifier is responsible for detailed native evidence.

SIGINT reaches the existing runner's `finally` cleanup in the same process.
The Gateway's bounded wait and process-group kill fallback remain unchanged.
No provisioning, account changes or resource deletion were added to this
service. The existing explicitly configured model stop and owned simulator
cleanup behavior remains in the runner.

## Scope and verification limits

The service supports the existing fixed Yokohama route only. Map-goal execution
continues through its existing path, including its prohibition on native models.
Rules, freshness checks, PX4 preparation/send deadlines, wait budgets and final
independent verifiers are unchanged. Service admission does not confer pad-entry
authority, override a Rules wait, or constitute a physical safety proof.

Offline tests exercise admission tampering, replay, interruption/failure,
approval/result binding, a real service subprocess with an isolated flight
runner fixture, and existing Gateway process cleanup. The fixture subprocess
does not constitute a PX4 flight, model inference or a native end-to-end pass.

Before claiming the separation works with real models, run one explicitly
approved, cost-bounded native fixed-route trial with a fixture mission judge.
Require actual AeroVLA and ANWM receipts at the existing bounded city checkpoints,
payload receipt, return and landing, all five independent verifiers, service
receipt binding, and verified model/simulator/cloud cleanup. A failed check is
a failed trial; report censored/incomplete outcomes without converting them to
success. This regression alone does not measure the model's incremental value
over a deterministic policy with the same information.
