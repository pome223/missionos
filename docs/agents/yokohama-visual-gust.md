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

`--visual-gust-trial {reach,reject,timeout}` requires a separately source-bound
approval, the pinned local image, the exact CPU fixture argument catalog and a
new output directory. Each immutable approval admits one flight and has an atomic
attempt claim. It uses four observations toward an eight-metre goal along the
frozen D1/D2 direction. Before flight, the full tracking corridor and vertical
landing column must pass the existing mapped clearance check.

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
