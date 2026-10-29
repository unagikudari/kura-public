# Kura 1.0 Standard — Volume 2: Database Specification

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** PostgreSQL domains, table responsibilities, constraints, indexes,
  migrations, provenance, data ownership, and the prohibition on authoritative
  JSON persistence.

## 1. Scope

This volume defines how canonical Kura 1.0 state is persisted. It states normative
requirements only; production DDL is **not** included in the foundation (ADR-0002,
KTS-HANDOFF-001 §9). `/database/schema.sql` is a placeholder until the existing
implementation is reconciled.

## 2. Authoritative persistence

- PostgreSQL MUST be the single authoritative store of canonical state.
- Canonical persistent state MUST be relational. JSON (`json`/`jsonb`) MUST NOT be
  used as canonical persistent state.
- JSON MAY be used only at transport or ephemeral boundaries (request/response
  bodies, queue payloads, caches) and MUST NOT be the system of record.

## 3. Domains and typing

- Continuous properties MUST be stored as typed scalar columns with, where
  practical, explicit interval/uncertainty companions rather than opaque blobs.
- Stable identifiers MUST use surrogate keys; natural keys MAY carry a UNIQUE
  constraint but MUST NOT be assumed immutable.
- Enumerated domains (statuses, kinds, region classes) MUST be represented as
  lookup tables or PostgreSQL enums, not as free text.

## 4. Table responsibilities (categories, not final schema)

- **Assets** — binary/derived asset records and their role relative to scenes.
- **Observations** — append-only evidence with producer/version/time provenance.
- **Resolved scenes** — versioned Resolver output, reconstructable.
- **Entities & attachments** — scene entities and the geometry/relations/regions/
  poses/attributes attached to them.
- **Projections** — derived, rebuildable materializations (tags, facets).

## 5. Constraints and integrity

- Every observation row MUST be immutable after insert; the schema SHOULD enforce
  this (e.g. revoke UPDATE/DELETE on observation tables from application roles).
- Provenance columns (producer, version, timestamp) MUST be NOT NULL on evidence
  tables.
- Foreign keys MUST enforce that attachments reference an existing scene entity.

## 6. Indexes

- Query paths for resolution and projection building SHOULD be supported by
  explicit indexes; index choices MUST be documented alongside their query.

## 7. Migrations

- Schema changes MUST be delivered as ordered, reviewable migrations under
  `/database/migrations/`.
- Migrations MUST be forward-only in production; a down path MAY exist for
  development.
- No production DDL MUST be added until the existing implementation is reconciled
  (KTS-HANDOFF-001 §9).

## 8. Data ownership

- Each table MUST have a documented owning component and a documented writer role.
- Application roles MUST have least-privilege grants; observation tables MUST be
  insert-only for writers.

## 9. Out of scope

Final column lists, exact index definitions, and reconciliation with the existing
implementation are follow-up work (KTS-HANDOFF-001 §13).
