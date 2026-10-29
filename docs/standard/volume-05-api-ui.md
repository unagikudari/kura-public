# Kura 1.0 Standard — Volume 5: API and UI Specification

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** REST interfaces, worker contracts, queue operations, viewer,
  annotation UI, generation UI, comparison UI, and search.

## 1. Scope

This volume defines the external contracts of Kura 1.0. It states requirements on
interfaces; it does not specify concrete route tables or component code.

## 2. REST interfaces

- REST endpoints MUST expose canonical state as resolved scenes and projections;
  they MUST NOT let clients write directly to observation tables.
- Write endpoints MUST express changes as new observations (append-only).
- Request/response bodies MAY be JSON (transport boundary); this JSON MUST NOT be
  persisted as canonical state (Volume 2).

## 3. Worker contracts

- Workers MUST claim units of work through a queue with explicit, auditable state
  transitions.
- A worker MUST record provenance (identity, version) on every observation or
  projection it writes.
- Worker output MUST be idempotent with respect to redelivery where practical.

## 4. Queue operations

- Queue operations MUST support claim, complete, retry, and dead-letter semantics.
- A claimed unit MUST have a visible owner and lease so stalled work is detectable.

## 5. UI surfaces

- **Viewer** — MUST present resolved scenes and assets; it MUST distinguish
  observed evidence from resolved truth.
- **Annotation UI** — MUST capture human input as comparison-based observations.
- **Generation UI** — MUST let a user define a generation target from a resolved
  scene without editing canonical state directly.
- **Comparison UI** — MUST show target-versus-observed differences for a generated
  asset.
- **Search** — MUST query derived projections/facets, not raw observations.

## 6. Consistency

- UI surfaces MUST make Resolver version and projection freshness visible where a
  stale read could mislead.

## 7. Out of scope

Concrete endpoint schemas, auth, and component implementation are follow-up work.
