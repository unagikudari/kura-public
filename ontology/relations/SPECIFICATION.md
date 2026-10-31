# Kura 1.0 — Relation Ontology Specification

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Relation ontology: relationships connecting scene entities (and their
  regions), their arity, directionality, and applicability.
- **Source:** [KTS-HANDOFF-001](../../docs/standard/KTS-HANDOFF-001-node-handoff-specification.md) §13, [Volume 3](../../docs/standard/volume-03-ontology.md), [Volume 7](../../docs/standard/volume-07-validation.md).

No production DDL (KTS-HANDOFF-001 §9). Seed under
[`/database/seed/relation-ontology/`](../../database/seed/relation-ontology/) is TSV,
JSON-free. Anatomical relations are modelled neutrally (ADR-0003).

## 1. Model

- Relations MUST be data-driven ontology entries with stable identifiers
  `kura:rel/<slug>`; renaming MUST NOT change the identifier.
- Relations MUST connect **scene entities** and, where required, their **regions**;
  they MUST NOT relate bare images.
- Each relation MUST declare `arity` (initially `binary`) and `directionality`
  (`directed` | `symmetric`).

## 2. Directionality and roles

- A `directed` relation MUST define a `subject` role and an `object` role (e.g.
  `grasping`: subject grasps object).
- A `symmetric` relation MUST NOT distinguish roles (e.g. `adjacent_to`); the two
  participants are interchangeable.
- Consumers MUST NOT infer direction from participant order for symmetric relations.

## 3. Categories

| Category | Meaning |
|---|---|
| `spatial` | Relative position/containment (proximity, adjacency, overlap). |
| `contact` | Physical contact/support between participants. |
| `interaction` | An entity acting on another entity/region. |

## 4. Applicability

- Each relation MUST declare which participant **kinds** (entity kind, region class,
  or region slug) may fill each role, as data.
- Applicability MUST be consistent with the body ontology (e.g. `contact` relations
  apply where `applicability.tsv` marks `relation`).
- Applicability MUST NOT be hardcoded in application source.

## 5. Validation linkage

- Contact/support/occlusion relations MUST be evaluable by Volume 7 (contact,
  support & gravity, occlusion constraints).
- A relation asserted on a scene MUST be expressible independently of a single asset
  so it can describe both a generation target and an observed output.

## 6. Observation and correction

- Relations are asserted by observations (ADR-0001); corrections are new
  observations; the Resolver derives current relations (Volume 4).

## 7. Seed

Baseline vocabulary ([`/database/seed/relation-ontology/`](../../database/seed/relation-ontology/)):
`relations.tsv` (slug, display, category, arity, directionality), 
`relation_applicability.tsv` (relation, role, participant_kind, participant_key).

## 8. Conformance / out of scope

Conforms if relations have stable ids, declared arity + directionality, role-typed
data-driven applicability, and no hardcoded lists in application source. Higher-arity
relations and full coverage are follow-up.
