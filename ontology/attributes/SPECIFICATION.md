# Kura 1.0 — Attribute Ontology Specification

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Attribute ontology: attributes that attach to scene entities and body
  regions, their value domains (categorical / continuous / boolean), and
  applicability.
- **Source:** [KTS-HANDOFF-001](../../docs/standard/KTS-HANDOFF-001-node-handoff-specification.md) §13, [Volume 3](../../docs/standard/volume-03-ontology.md).

No production DDL (KTS-HANDOFF-001 §9). Seed under
[`/database/seed/attribute-ontology/`](../../database/seed/attribute-ontology/) is
TSV, JSON-free. Attributes of anatomy are modelled neutrally (ADR-0003).

## 1. Model

- Attributes MUST be data-driven ontology entries with stable identifiers
  `kura:attr/<slug>`; renaming MUST NOT change the identifier.
- Attributes MUST attach to **scene entities** or **body regions**, never to bare
  images (INV-5).
- Each attribute MUST declare a **value type**: `categorical`, `continuous`, or
  `boolean`.

## 2. Value domains

- `categorical` attributes MUST enumerate their allowed values as data (an attribute
  value set); values MUST have stable slugs.
- `continuous` attributes MUST declare a numeric range and MAY declare a unit; per
  Volume 1 §6 a continuous observation MUST be able to carry a scalar estimate with
  an interval/uncertainty, not a bare point.
- `boolean` attributes are present/absent style and MUST NOT be used where a
  categorical or continuous domain is more truthful.

## 3. Categories

| Category | Meaning |
|---|---|
| `appearance` | Stable visual properties of an entity/region. |
| `state` | Transient condition of an entity/region. |
| `measure` | Continuous measured/estimated quantity. |

## 4. Applicability

- Each attribute MUST declare what it applies to, by region **class**, region
  **slug**, or entity **kind** (e.g. `person`). Class-level applicability applies to
  all regions of that class unless a narrower rule overrides.
- Applicability MUST be data; it MUST NOT be a hardcoded branch in application source.
- Attribute applicability MUST be consistent with the body ontology's own
  applicability rules (`/database/seed/body-ontology/applicability.tsv`).

## 5. Observation and correction

- An attribute value on an entity/region is asserted by **observations** (ADR-0001);
  a correction is a new observation. The Resolver derives the current value
  (Volume 4).
- For continuous attributes the Resolver MAY combine scalar estimates; the ontology
  only defines the domain, not the resolution policy.

## 6. Seed

Baseline vocabulary ([`/database/seed/attribute-ontology/`](../../database/seed/attribute-ontology/)):
`attributes.tsv` (slug, display, category, value_type, applies_kind, applies_key,
range_min, range_max, unit), `attribute_values.tsv` (categorical value sets).

## 7. Conformance / out of scope

Conforms if attributes have stable ids, a declared value type + domain, and
data-driven applicability, with no hardcoded lists in application source. Full
attribute coverage and resolution policies are follow-up.
