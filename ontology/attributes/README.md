# Attribute Ontology

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Attribute ontology for Kura 1.0. Structure and requirements only; the
  full dataset is follow-up work (KTS-HANDOFF-001 §13).

## 1. Requirements

- Attributes MUST be data-driven ontology entries with stable identifiers; they
  MUST NOT be hardcoded in application source.
- Attributes MUST attach to scene entities (INV-5), not to bare images.
- Attribute applicability (which entity kinds/regions an attribute applies to) MUST
  be expressed as data.
- Continuous attributes MUST support scalar estimates with intervals/uncertainty
  where practical (INV — Volume 1 §6), rather than boolean-only encodings.

## 2. Categories (non-final)

- Appearance attributes of entities and regions.
- State attributes (transient conditions of an entity).
- Continuous attributes (measured/estimated quantities).

## 3. Formal specification and seed

Formal spec: **[SPECIFICATION.md](./SPECIFICATION.md)** (value domains, applicability,
observation model). Baseline seed: **[`/database/seed/attribute-ontology/`](../../database/seed/attribute-ontology/)**
(TSV, JSON-free; no production DDL, KTS-HANDOFF-001 §9).
