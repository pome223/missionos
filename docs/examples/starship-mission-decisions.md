# MissionOS decides within an approved mission scope

This development slice connects decisions across a Starship-inspired flight:
start deployment, monitor the first release, request a mechanism diagnostic if
needed, select terminal return guidance, and decide capture or diversion.
The operator approves the scope before flight. MissionOS then chooses within
it; an independent check constrains each execution and later observations
show what actually changed.

The five conditions cover normal operation, a release fault, fuel loss, tower
unavailability and a deployment suspension notice. Each has the same-start
fixed-timeline comparison. Normal operation should remain unchanged; abnormal
conditions must meet the stated outcome comparison before this slice is called
complete. Record verification alone is insufficient.

Real Jev inference now changes decisions and a recorded return outcome. In the
release-fault run it chose to collect mechanism status, stopped deployment after
the blocked report, selected retained-payload return guidance and chose diversion.
The same-start fixed timeline impacted at 238.74 m/s; the managed flight contacted
the surface at 3.22 m/s. This is low-speed contact, not a validated landing.

The operating goal remains **unmet**. In a normal live run, one provider response
failed format validation. The preapproved fallback stopped after one release;
retained-payload guidance then impacted at 619.78 m/s. Scripted decisions passed
three of five condition comparisons: normal, release fault and tower
unavailability. The suspension-notice return impacted; fuel loss reached the
integration horizon without returning. These failures remain failures.

The two live runs attempted nine Jev calls, eight with validated inference
responses. DeepSeek was not routed. This shows model decisions affecting the
simulation, not superiority over a conventional flight manager. The scripted
policy achieved the same release-fault improvement. Human workload was not
measured.

The subsequent v2 contract addresses the response-error path: when current
the persistent constraints permit the approved nominal sequence, a rejected or missing
AI reply preserves that sequence and the initial return plan. It does not
automatically stop deployment or select different return guidance. Initial
timeout handling resolves before the first release slot. Interlocks, unresolved
notices and previously stopped sequences still prevent continuation. This
requires a fresh plan and approval; it is not an unconditional safety guarantee.
Temporary release-gate excursions skip individual slots without permanently
aborting a running sequence. The gate still applies to every actual release.

Four pre-review v2 six-DOF response-failure fixtures preserve the exact baseline
terminal metrics: invalid monitoring reply, invalid return reply, missing initial
reply and missing monitoring reply. Each releases all 26 payloads and contacts
at 4.55 m/s. The monitoring-error fixture also passes the Gateway approval,
separate worker, later-observation and artifact-verification chain.
[Response-fallback evidence](../assets/starship-mission-decisions-20261007/fallback-summary.json)
is separate from the older live inference records. No new model calls occurred
in these regressions. Three archived CLI pairs were not rechecked by the final
verifier; their grants and source snapshots remain historical. Separate final
Gateway records cover the current monitoring-error path. These checks do not
qualify other return conditions.
The original five-condition gate was not rerun under v2.

Combining a release fault with an invalid return reply still impacts at
238.74 m/s on the initial fixed plan. It is worse than the old 3.22 m/s retained
guidance result in that single case. Keeping the initial plan avoids an automatic
switch to an unqualified controller; it does not solve payload-retained recovery.

The 26-versus-25 retained-payload discrepancy remains unresolved. The saved
25-payload return loses attitude tracking around 50 km and never enters the
terminal burn. The single 26-payload low-speed contact therefore establishes
neither robust retained-payload recovery nor an operational fallback.

This is a development simulation. Fixture decisions are scripted, not AI.
The live option uses Jev and conditional DeepSeek within the displayed budget.
Satellite bodies are generic rigid payloads; no unfolding, communications or
orbit raising is modeled. Launch-derived catch remains unqualified, and the
declared diversion target is not a verified safe landing site. Actual outcomes,
including unsuccessful returns and comparisons, belong beside the demo.

The [public evidence summary](../assets/starship-mission-decisions-20261006/evidence-summary.json)
preserves the measured outcomes and exact source snapshots without raw sessions
or credentials. Later pending-start and observation-inventory checks do not
rewrite those historical runs.

See the [operating contract and runtime commands](../agents/starship-mission-director.md).
