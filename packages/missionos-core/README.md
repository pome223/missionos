# missionos-core

Shared MissionOS schemas, evidence semantics, action-feasibility contracts, and
claim semantics.

This package should stay independent of CLI, Gateway server internals, simulator
adapters, and hardware adapters.

Its Action Feasibility API owns source references, freshness, opaque observation
cursors, policy/model binding, tri-state results, and authority-free
revalidation artifacts. Geometry, terrain, and vehicle-performance calculations
remain in registered backend extensions.
