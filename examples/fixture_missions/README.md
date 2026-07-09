# Fixture Missions

Fixture missions are deterministic public-safe records used for CLI, Gateway,
map, and contract smoke tests.

Do not place private task database exports or generated output snapshots here
without publication review.

## Nvblox

`nvblox/available-perception-evidence.json` and
`nvblox/unavailable-missing-pose-evidence.json` are public-safe perception
evidence fixtures. They are not live Isaac ROS Nvblox output and must not be
described as runtime execution.

Use them to validate claim boundaries:

- available perception evidence may support `costmap_obstacle_observed=true`
- perception evidence alone must not claim obstacle avoidance, dispatch,
  physical execution, or delivery completion
- missing depth/pose/reconstruction/costmap observations must fail closed with
  explicit blocking reasons
