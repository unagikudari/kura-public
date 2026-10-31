# ADR-0001: Observations Are Immutable

- **Status:** Accepted
- **Version:** 0.1.0
- **Date:** 2026-07-26
- **Scope:** The evidence model for all model and human observations in Kura 1.0.

## Context

Kura 1.0 derives canonical truth from evidence via a versioned Resolver
(ADR — see Volume 1). If evidence could be edited in place, provenance and
reproducibility would be lost and model/human disagreement would be unrecoverable.

## Decision

- Observations MUST be append-only. Once written, an observation MUST NOT be
  updated or deleted by application roles.
- A correction MUST be expressed as a **new** observation that references what it
  corrects; the prior observation MUST remain intact.
- Every observation MUST record producer (model or human), version, and time.

## Consequences

- Full audit and reproducibility of resolution.
- Storage grows monotonically; retention/compaction is a separate, non-destructive
  concern (MUST NOT violate immutability).
- The database schema SHOULD enforce immutability (revoke UPDATE/DELETE on
  observation tables from application roles).

## Alternatives rejected

- **Mutable current-state tags as truth** — rejected; loses provenance and
  conflict history.
