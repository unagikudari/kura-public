# Identity Ontology (character / franchise / species)

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** The identity/reference layer for entities — species, named character,
  and franchise — distinct from the physical ontologies. Formal spec:
  **[SPECIFICATION.md](./SPECIFICATION.md)**.

## 1. Requirements

- Species/character/franchise MUST be data-driven entries with stable identifiers
  (`kura:species/…`, `kura:character/…`, `kura:franchise/…`); not hardcoded in
  application source.
- **Identity is a reference, not a physical assertion**: a character records *who* an
  entity is; the entity's actual attributes/body/pose are observed separately. A
  depiction may deviate from a character's canonical traits.

## 2. Why this exists

The [database reconciliation](../../database/RECONCILIATION.md) flagged character
tags (mash_kyrielight…) and species/kind tags (monster_girl, mermaid) with no target.
This ontology is that target: a bounded **species** taxonomy plus open **character** /
**franchise** registries, seeded representatively (the full catalog is data, not
enumerated here).

## 3. Formal specification and seed

Spec: **[SPECIFICATION.md](./SPECIFICATION.md)**. Seed:
**[`/database/seed/identity-ontology/`](../../database/seed/identity-ontology/)**
(TSV, JSON-free; characters reference valid franchise + species; no production DDL,
KTS-HANDOFF-001 §9).
