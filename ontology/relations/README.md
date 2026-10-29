# Relation Ontology

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Relation ontology for Kura 1.0. Structure and requirements only; the
  full dataset is follow-up work (KTS-HANDOFF-001 §13).

## 1. Requirements

- Relations MUST be data-driven ontology entries with stable identifiers; they
  MUST NOT be hardcoded in application source.
- Relations MUST connect scene entities (and, where required, their regions), not
  bare images.
- Relation applicability (which entity/region kinds a relation may connect) MUST be
  expressed as data.
- Directional relations MUST encode direction explicitly.

## 2. Categories (non-final)

- Spatial relations (relative position, containment).
- Contact relations (touching, support).
- Interaction relations between entities.

## 3. Formal specification and seed

Formal spec: **[SPECIFICATION.md](./SPECIFICATION.md)** (arity, directionality/roles,
applicability, validation linkage). Baseline seed:
**[`/database/seed/relation-ontology/`](../../database/seed/relation-ontology/)**
(TSV, JSON-free; no production DDL, KTS-HANDOFF-001 §9).
