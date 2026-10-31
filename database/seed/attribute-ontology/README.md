# Attribute Ontology Seed

- **Status:** Draft — **Version:** 0.1.0
- **Scope:** Baseline attribute vocabulary per
  [`ontology/attributes/SPECIFICATION.md`](../../../ontology/attributes/SPECIFICATION.md).
  TSV, JSON-free authoring data; no production DDL (KTS-HANDOFF-001 §9). Anatomical
  attributes modelled neutrally (ADR-0003).

| File | Describes |
|---|---|
| `attributes.tsv` | attribute: slug, category, value_type, applicability (class/region), range/unit |
| `attribute_values.tsv` | categorical value sets |

**Stable id:** `kura:attr/<slug>`. `applies_kind`/`applies_key` reference **body
ontology** classes/regions and resolve against `database/seed/body-ontology/`
(verified at authoring time). Continuous attributes carry `range_min`/`range_max`
and `unit`; observations may attach scalar estimates with intervals (Volume 1 §6).

**Gap-fill (reconciliation):** adds `breast_size`, `penis_size`, `erection_state`,
and `facial_expression` attributes, and extends `hair_color` / `eye_color` /
`hair_style` / `skin_tone` value sets — resolving legacy tags (large_breasts, erection,
blue_hair, bangs, ahegao…) that previously had no attribute target.
