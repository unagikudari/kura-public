# ADR-0002: Canonical Persistence Is Relational

- **Status:** Accepted
- **Version:** 0.1.0
- **Date:** 2026-07-26
- **Scope:** How canonical Kura 1.0 state is persisted.

## Context

Canonical state (observations, resolved scenes, entities and their attachments)
needs constraints, referential integrity, and queryable structure. Authoritative
JSON blobs cannot guarantee these.

## Decision

- PostgreSQL MUST be the authoritative store.
- Canonical persistent state MUST be relational.
- JSON (`json`/`jsonb`) MUST NOT be used as canonical persistent state.
- JSON MAY be used only at transport or ephemeral boundaries (API bodies, queue
  payloads, caches) and MUST NOT be the system of record.

## Consequences

- Schema-enforced integrity and typed, indexable state.
- More up-front schema/migration work than a document store.
- `/database/schema.sql` remains a placeholder until the existing implementation is
  reconciled; production DDL MUST NOT be added before then (KTS-HANDOFF-001 §9).

## Alternatives rejected

- **JSON documents as canonical state** — rejected; no enforced integrity, weak
  queryability, silent drift.
