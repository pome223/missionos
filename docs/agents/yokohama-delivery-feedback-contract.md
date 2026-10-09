# Full delivery feedback trial

This is a separately approved simulation contract. The previous inland endpoint
and candidate recovery contracts retain their 900-second mission and 220-second
recovery limits. No physical vehicle or recipient is involved.

The authored stationary ship route is ship, coast, D1, D2, D3, delivery pad,
observed cargo receipt, D3, D2, D1, coast, ship, landing and disarm. The ship is
1,000 m offshore from the authored coast, and the coast is 400 m from D1. Wind
is zero. Every authored hover retains a 30-second observation window. The full
mission uses a fixed 2,100-second deadline and a separate fixed 500-second abort
reserve for the 1,400 m return to the ship. These longer absolute limits account
for the added route; they do not change any existing trial contract.

Only at D1 may the model propose two legs toward the four-metre inland endpoint.
The original 3 m candidate limit, two VLA/two WAM request caps, 0.5 m goal
tolerance, map clearance and tracking envelope remain unchanged. The distance
adapter validates the original VLA candidate before shortening it. After model
completion the authored feedback exit returns to D1. The AP then executes the
authored delivery route; model control is revoked before pad waiting or release.

CPU-only scenarios are `delivery`, `wait`, `candidate_rejected`, and `timeout`.
The rejection fixture proposes a five-metre second leg; the timeout fixture
delays the second VLA response past the unchanged 75-second exchange deadline.
Both faults occur after an observed first leg. Recovery first revokes model
authority, then follows the original D1/coast/ship line and lands/disarms.
Recovery success does not turn the failed delivery mission into success.
The trial mounts the unchanged cargo box above the landing gear; a rejected
delivery returns with the cargo attached and still requires fresh gear/deck
contact. It does not detach undelivered cargo to satisfy landing verification.
The wait scenario proves pad waiting after revocation and prevents late model
responses from re-entering control. These tests do not qualify arbitrary
failures outside the D1 model boundary or moving ships, weather, or real coastlines.

`yokohama_sitl.py --delivery-trial` requires a source-bound exact proposal,
approval manifest, pinned local simulator image and fresh per-job attempt claim.
The exact argument catalog rejects extra switches. Fault and wait scenarios
cannot use native model services. Native `delivery` requires explicit service
configuration captured and hashed at admission. Running the simulator or paying
for remote models remains opt-in and separately authorized.

Run the offline verifier after owned simulator cleanup:

```sh
python scripts/verify_yokohama_delivery_trial.py RUN --output VERDICT.json
```

It binds approved source snapshots, map/route and model assets, raw PX4 state,
independent Gazebo poses, altitude transport and all command/receipt events.
Success additionally passes the complete two-leg endpoint and full SITL
verifiers. Faults require a complete independently checked first model leg,
the observed second-leg rejection/timeout, irrevocable revocation, each return
leg, the fixed deadline and fresh deck contact with landed/disarmed telemetry.
Simulated recipient receipt and native inference are distinct facts; neither
implies physical delivery or direct control by unmodified model output.
