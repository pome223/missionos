# Fixture Missions

Fixture missions are deterministic public-safe records used for CLI, Gateway,
map, and contract smoke tests.

Do not place private task database exports or generated output snapshots here
without publication review.

`ship_delivery/` contains small scenario overrides for the stationary-ship
kinematic fixture: clear, short blockage (wait), and long blockage (detour).
Run them with `missionos ship-delivery run --scenario <path> --approve-fixture`.
These fixtures invoke no PX4, Gazebo, hardware, VLA, or WAM.
