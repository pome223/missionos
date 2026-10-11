# CPU visual-input and eight-metre gust experiment

This is a separate opt-in experiment based on the completed delivery revision.
The existing two-cycle, four-metre native contract and the ship-delivery catalog
keep their original limits. This experiment does not enable native models.

`run_yokohama_visual_pair.py --approve-gazebo --local-image-id IMAGE --output-dir NEW`
renders the actual frozen Yokohama scene without aircraft/PX4. The forward and
downward cameras use one fixed replica of the onboard viewpoint. The same
eight-metre goal and prompt are used with a clear passage and a static red panel.
Case labels, barrier coordinates and timing remain in evaluation metadata. The
proposal adapter receives only the two RGB byte strings and the identical prompt.

`yokohama.visual-input-hold.v1` is a proposal-only contract. It admits a level,
heading-preserving hold (`0 49 49`) separately from the existing forward parser.
It permits at most three observations, two reobservations and twenty seconds.
Freshness, ordered timestamps and a two-second response lease are checked before
accepting raw candidates. Expiry, replay, invalid output and the third hold close
the session without dispatch authority. LAND and rotation remain excluded.
The enabled CPU color double detects only the staged red panel. An image-dependent
CPU output proves interface wiring, not learned AeroVLA perception or avoidance.

`--visual-gust-trial {calm,reach,reject,timeout,return_fault}` requires a separately source-bound
approval, the pinned local image, the exact CPU fixture argument catalog and a
new output directory. Each immutable approval admits one flight and has an atomic
attempt claim. It uses four observations toward an eight-metre goal along the
frozen D1/D2 direction. Before flight, the full tracking corridor and vertical
landing column must pass the existing mapped clearance check.

The CPU continuation first qualifies the complete eight-metre route without
wind (`calm`). Initial spawn, takeoff and D1 hold use the approved D1/D2 heading;
the zero horizontal takeoff displacement must not introduce a zero-degree
bearing. Before arming, three distinct fresh ground observations spanning two
simulation seconds must show stationarity and world/estimator heading agreement
within the existing 45-second startup alignment wait. This changes initialization
only; it does not add continuous ground-truth heading compensation. The previous
unaligned run failed its first-leg settling deadline and safely recovered. Its
evidence remains retained. A gust run requires a passing calm run of the same
runtime source closure and pinned image.

After AP return, the fixed thirty-second hold starts only after fresh in-envelope
stationarity: three distinct pose/time observations spanning two seconds, within
0.25 m horizontally and 0.15 m vertically, speed at most 0.3 m/s and AUTO_LOITER.
The existing minimum three-second settling and maximum twenty-second settling
wait remain unchanged. Return failures after model revocation in this CPU scope
use the existing separately approved 220-second recovery, with no new model
authority or relaxed envelope. The `return_fault` fixture follows the identical
route/pulse and deliberately fails after the measured return hold to exercise
this failure path. Successful recovery preserves the failed mission outcome.

After the first stable leg, Gazebo receives exactly one 0.5 m/s eastward wind
pulse lasting two simulation seconds, followed by confirmed zero wind. PX4 hold
must settle within the existing envelope before fresh images are requested.
A wind-enabled witness and an otherwise identical disabled control distinguish
actual simulated force from merely recording a requested wind value. VLA/WAM
does not provide the gust controller.

The candidate corridor (0.5 m), tracking tube (1 m), obstacle clearance (2 m),
goal tolerance (0.5 m), individual segment maximum (3 m), altitude bounds,
75-second response limit and 600-second model-session deadline are unchanged.
The new inland run is bounded by 900 seconds. The optional rejection/timeout
scenarios revoke model authority and use the existing fixed 220-second recovery.
Return and terminal landing/disarm require three distinct samples spanning two
simulation seconds. No retry extends a deadline. The original mission remains
failed when a fault scenario recovers successfully.

`verify_yokohama_visual_gust.verify(RUN)` independently reopens source/config/world
hashes, altitude-upload bindings, mailbox permits, trajectory, wind/control
observations, fresh post-gust captures, revocation, raw landed/arming topics and
contact events. Raw evidence is local simulator data rather than authenticated
physical telemetry. An approved geometry gate remains independent of proposal
generation.

GPU, paid resources, external model API calls, publishing and deployment are
outside this implementation's execution scope. A future native trial needs a
separate reviewed adapter/decoder and approval; four VLA/WAM requests are a budget
proposal, not native qualification. Preserve every historical failure, immutable
approval, attempt ledger and source binding. Archive and verify restores before
removing owned stopped containers; retain shared images and the unpublished branch.
