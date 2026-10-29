# Kura 1.0 — Object Ontology Specification

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Objects — clothing, accessories, props, furniture, and environment —
  as scene entities, their taxonomy, the body regions clothing **covers**, object
  states, and how objects participate in a scene. Closes the `object` gap surfaced
  by [database reconciliation](../../database/RECONCILIATION.md) §6/§9.
- **Source:** [KTS-HANDOFF-001](../../docs/standard/KTS-HANDOFF-001-node-handoff-specification.md) §13, [Volume 3](../../docs/standard/volume-03-ontology.md).

No production DDL (KTS-HANDOFF-001 §9). Seed under
[`/database/seed/object-ontology/`](../../database/seed/object-ontology/) is TSV,
JSON-free authoring data. Objects are modelled neutrally.

## 1. Model

- Objects MUST be data-driven ontology entries with stable identifiers
  `kura:object/<slug>`; renaming MUST NOT change the identifier.
- An object is a scene **entity of kind `object`** (distinct from `person`), so
  geometry, attributes, and relations attach to it the same way (Volume 3, INV-5).
- Each object MUST declare a **category** and, for clothing, a **slot** (§2, §3).

## 2. Categories

| Category | Meaning | Typical participation |
|---|---|---|
| `clothing` | Worn garment | `wearing` (person → clothing) |
| `accessory` | Worn non-garment (jewelry, ornament, mask) | `wearing` |
| `prop` | Held/used object | `holding` / `using` |
| `furniture` | Scene furniture | `present_in_scene` |
| `environment` | Environment/backdrop element | `present_in_scene` |

- New categories MUST be added as data with rationale, not hardcoded in application
  source.

## 3. Clothing slots and coverage

- Every `clothing` (and slot-bearing `accessory`) object MUST declare a **slot** from
  a fixed set: `headwear`, `eyewear`, `neckwear`, `topwear`, `bottomwear`, `legwear`,
  `footwear`, `handwear`, `underwear`, `outerwear`, `full_body`.
- A clothing object MUST declare the body regions/classes it **covers**, referencing
  **body-ontology** slugs/classes (`object_coverage.tsv`). Coverage is what lets
  validation and generation reason about occlusion (Volume 7) and inpaint targets
  (Volume 8).
- Coverage is a default; a specific scene MAY override it via a state (§4, e.g.
  `open`, `displaced`, `lifted`).

## 4. Object state

- Object presence/configuration on an entity is asserted by **observations**
  (ADR-0001); a correction is a new observation.
- The ontology MUST define an enumerated set of object states. Initial set
  (`object_states.tsv`): `worn`, `removed`, `partially_removed`, `open`,
  `displaced`, `lifted`, `torn`, `transparent`.
- State modifies effective coverage: e.g. `removed` ⇒ no coverage; `transparent`
  ⇒ region is covered but visible; `displaced`/`lifted` ⇒ partial coverage.
- Flat legacy tags that are really object **states** (e.g. `no_panties`, `open_shirt`,
  `torn_pantyhose`, `panty_peek`) MUST reconcile to `(object, state)`, not to a
  separate object.

## 5. Participation relations

- Objects participate via relations that extend the [relation ontology](../relations/SPECIFICATION.md):
  - `wearing` — directed, subject `person` → object `clothing`/`accessory`.
  - `holding` — directed, subject `person` (region `hand`) → object `prop` (reuses
    the relation-ontology `holding`/`grasping`).
  - `using` — directed, subject `person` → object `prop`.
  - `present_in_scene` — directed, subject `scene` → object.
- These MUST be data (`object_relations.tsv`) and SHOULD be folded into the relation
  ontology when it is next revised.

## 6. Object attributes

- Objects MAY carry [attribute-ontology](../attributes/SPECIFICATION.md) values
  (e.g. colour, material) plus object-specific attributes (transparency) as data;
  attribute terms live in the attribute ontology, not here.

## 7. Seed

Baseline vocabulary ([`/database/seed/object-ontology/`](../../database/seed/object-ontology/)):
`object_categories.tsv`, `clothing_slots.tsv`, `objects.tsv`,
`object_coverage.tsv` (→ body regions/classes), `object_states.tsv`,
`object_relations.tsv`. Grounded in real legacy clothing/prop tags.

## 8. Conformance / out of scope

Conforms if objects have stable ids, a category (+slot for clothing), coverage
resolving to body-ontology entries, and data-driven states/relations, with nothing
hardcoded in application source. Full clothing/prop coverage, material ontology, and
folding participation relations into the relation ontology are follow-up.
