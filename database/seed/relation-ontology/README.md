# Relation Ontology Seed

- **Status:** Draft — **Version:** 0.1.0
- **Scope:** Baseline relation vocabulary per
  [`ontology/relations/SPECIFICATION.md`](../../../ontology/relations/SPECIFICATION.md).
  TSV, JSON-free authoring data; no production DDL (KTS-HANDOFF-001 §9). Anatomical
  relations modelled neutrally (ADR-0003).

| File | Describes |
|---|---|
| `relations.tsv` | relation: slug, category, arity, directionality |
| `relation_applicability.tsv` | per-role participant kinds (entity/class/region) |

**Stable id:** `kura:rel/<slug>`. Directed relations define `subject`/`object`
roles; symmetric relations use interchangeable `participant`. Applicability
`participant_key` resolves against **body ontology** classes/regions or entity kinds
(`person`/`entity`/`any`) — verified at authoring time. Contact/occlusion relations
feed Volume 7 validation.
