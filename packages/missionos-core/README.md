# missionos-core

Shared MissionOS schemas, evidence semantics, action-feasibility contracts, and
claim semantics. This includes a backend-neutral five-axis Repair diagnostic
that keeps action activity, corrective alignment, predicate recovery,
preservation, and stable hold separate.

This package should stay independent of CLI, Gateway server internals, simulator
adapters, and hardware adapters.

Its Action Feasibility API owns source references, freshness, opaque observation
cursors, policy/model binding, tri-state results, and authority-free
revalidation artifacts. Geometry, terrain, and vehicle-performance calculations
remain in registered backend extensions.

Repair diagnostics accept model output only for action activity. Corrective
alignment requires a registered diagnostic reference or observed effect, while
predicate recovery, preservation, and stable hold require simulator, external
runtime, or physical observations. A passing report remains bounded to its
declared executor, task, fixture, and evaluation scope and creates no authority.
