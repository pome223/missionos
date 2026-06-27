# Artifact Taxonomy

MissionOS should make evidence easy to audit.

## Task Record

The durable task record is the main object for status, kind, title, artifacts,
and high-level outcome fields.

## Timeline

Timeline events are ordered evidence. They should identify what happened, when,
and which boundary produced the event.

## Runtime Snapshot

Runtime snapshots describe observed simulator or hardware state. Treat stale,
missing, or partial snapshots as limited evidence.

## Map Artifact

A map artifact displays route, location, telemetry, or replay data. It is a
read-only evidence surface, not a dispatch surface and not a delivery verifier by
itself.

## Fixture Artifact

Fixture artifacts are public-safe sample records. They should be deterministic,
small, and free of private paths or credentials.
