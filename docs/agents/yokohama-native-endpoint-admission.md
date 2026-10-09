# Adapted native endpoint admission

The dedicated path is `scripts/yokohama_native_endpoint_trial.py`. Its default
`--plan` action only validates and describes local inputs. `--execute` requires
an exact-plan approval, USD 8 reservation and successful same-source CPU-double
flight with independently observed goal arrival, exit, landing and disarm.
A previous CPU rejection-return result cannot satisfy that gate. No new flight
or paid resource is qualified by the current offline development.

The underlying SITL catalog is exactly `--phase flight --endpoint-feedback
--goal-distance-adapter --endpoint-adapter-trial --decision-backend fixture|native
--wam-profile motion-v4 --timeout-seconds 900 --local-image-id <pinned-id>`.
Native adds one approved service file. Sea, payload, wind, fault injection and
physical execution are excluded. Admission binds the full source closure, image,
fixed feedback/recovery policies, adapter policy, exact arguments and captured
service bytes. The approval file is captured and rechecked; the atomic persistent
flight claim allows at most one attempt, including failures before launch.

The dedicated CPU double emits fixed `58 49 49` then `43 49 49` actions;
the second overshoot must be shortened by the explicit adapter. It does not
select its bins from the remaining goal distance. Other fixture modes retain
their prior behavior.

Both backends use the same controller, original Rules and separate fixed vehicle
return authority. Candidate failure, non-goal completion or endpoint deadline
failure revokes model authority before fixed launch-point return, landing and
measured disarm. The runtime retains 220 seconds for this operation, without
relaxing route, altitude, freshness, progress or stable hold limits. It issues at
most two VLA and two WAM requests. Model stop is required before normal AP exit.

`verify_yokohama_native_endpoint.py` binds operator approval, runtime image,
config, copied sources, request/response hashes and revocation. It combines the
endpoint verifier with the complete SITL/altitude/landing verifier on success,
or the independent recovery verifier on failure. Missing cleanup, late dispatch,
unconfirmed remote model stop or altered evidence fails closed. Native service
identity and raw model outputs are checked by the endpoint verifier. Adapter
control never qualifies raw-output control or delivery. A recovery verdict only
qualifies recovery; the original mission remains failed.

Cloud preparation uses one standard Oregon L4 VM, one 200 GiB balanced auto-delete
boot disk, ephemeral IPv4, existing SSH key and loopback tunnels. Upload is exactly
the nine reviewed service/bootstrap/lifecycle/adapter-weight files. Public pinned
model revisions are downloaded on the VM. Requests contain approved goals and
current/past onboard RGBD observations, never future truth or a collision-map
oracle. No service account, scopes, new firewall, NAT, snapshots or cloud storage.

The fresh plan binds payload/source hashes and has one budget claim and create.
Provider termination is DELETE at an absolute deadline no later than 3600 seconds
from reservation. Flight requires at least 1500 seconds remaining (900 flight plus
600 cleanup). Tunnel failure or less than 600 seconds remaining stops the trial
without retry. Model readiness has the inherited bounded bootstrap timeout.
Finally, stop owned processes, collect bounded evidence and delete only recorded
numeric VM/boot-disk identities; verify absence. Keep evidence and unpushed Git.
Cleanup uncertainty is reported and prevents a new attempt. Prices, project,
resource names, paths and raw evidence stay in the private local plan, not Git.

USD 8 is an authorization/reservation, not a provider-enforced monetary cap.
Cost estimates include compute/GPU, disk, IPv4 and egress reserve. They exclude
unconfirmed taxes, currency conversion and excess egress. No free allowances are
assumed. The current execution gate is the single CPU connection flight;
one CPU connection flight before the single paid trial; dedicated USD 8 approval
has been received. Live evidence remains required.

On the owned VM only, IPv4 non-loopback packet quotas are installed before
upload/bootstrap: 1 GiB outbound, 64 GiB inbound. Global IPv6 addresses cause
admission failure. Quota exhaustion blocks traffic and requires cleanup through
the independent GCP API. No firewall or network policy of a persistent resource
is changed. Provider DELETE and disk auto-delete remain required.
