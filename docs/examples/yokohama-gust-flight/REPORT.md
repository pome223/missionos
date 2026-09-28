# Seeded gusts and direct sea endpoints

The previous offshore stall was avoided by replacing 50 unnecessary sea fly-through waypoints with one endpoint plus loiter, without relaxing wind, arrival, hold or receipt bounds. In the common simulator window 150–240 s, observed ground speed changed from 0.583 to 2.931 m/s; the trajectory target changed from 0.582 to 8.000 m/s. The coastal 30-second hold passed. This is one bounded rerun, not a PX4-wide root-cause proof or airframe wind qualification.

Seed 20260928 fixes gust onset, duration and total speed before launch. The production Gazebo publisher, acknowledgement and independent witness/control force boundary passed a separate marker-only smoke. Real PX4/Gazebo gust flight recorded IDs [0, 1, 2, 3, 4, 5, 6, 7, 8] in zones ['offshore', 'harbor', 'coast', 'city']. **Delivery and return remain incomplete**: steady trial stopped at a CPU fixture WAM visible-structure gate; gust trial ended at 00-D1 (ValueError: City decision hold, reserve, heading or estimator continuity lost). Full-flight/cargo verifiers cannot certify missing completion evidence. All failures remain recorded.

No native VLA/WAM or GPU was invoked. Incremental GPU cost $0; previous estimated cumulative cost $16.7063 of $17. Video combines original recorded RGB, time-based SITL battery telemetry, and previously confirmed wind seed receipts. No local-flow or power measurement is claimed.

[Detailed Japanese report](REPORT-ja.md) · [Replay and videos](index.html) · [Raw-result summaries](summary.json) · [Analysis and source hashes](ap-analysis.json) · [All attempts](development-attempts.json).

The last city seed was 9.153 m/s. The decision hold rejected observed speed 0.364 m/s above its 0.3 m/s limit; anchor drift was only 0.083 m. The final pulse has no force/recovery observation window, so the whole-period wind verifier remains failed. Only the [completed gust prefix](completed-gust-prefix.json), IDs 0–7, passes force verification. Worker stop acknowledgement also failed the motion gate; host finally cleanup separately confirms the fixture stopped and the container was removed.
