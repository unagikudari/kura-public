# Kura 1.0 — Identity Ontology Specification (character / franchise / species)

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** The **identity / reference** layer for entities — species (kind of
  being), named **character** identity, and the **franchise** a character belongs to
  — distinct from the physical ontologies (body / attribute / pose). Closes the
  `character` gap from [database reconciliation](../../database/RECONCILIATION.md)
  §4/§6.
- **Source:** [KTS-HANDOFF-001](../../docs/standard/KTS-HANDOFF-001-node-handoff-specification.md) §13, [Volume 3](../../docs/standard/volume-03-ontology.md).

No production DDL (KTS-HANDOFF-001 §9). Seed under
[`/database/seed/identity-ontology/`](../../database/seed/identity-ontology/) is TSV,
JSON-free authoring data.

## 1. Model — identity is reference, not physique

- Identity concepts attach to a scene **entity** (usually a `person`):
  - **species** — the kind of being (human, elf, catgirl, mermaid…).
  - **character** — a named character identity (open registry).
  - **franchise** — the work/copyright a character belongs to (open registry).
- Each identity concept MUST be a data-driven entry with a stable identifier
  (`kura:species/<slug>`, `kura:character/<slug>`, `kura:franchise/<slug>`);
  renaming MUST NOT change the identifier.
- **Identity is a reference, not a physical assertion.** A character has *canonical*
  traits, but a specific depiction MAY deviate. The character identity records **who**
  the entity is; the physical **attributes / body / pose** of that depiction are
  observed separately (per Volume 4). A character drawn with non-canonical hair is
  still that character by identity, and the attribute observation records the actual
  hair. Consumers MUST NOT infer physical attributes from a character identity.

## 2. Species

- Species is a **bounded taxonomy** of person-entity kinds; new species are added as
  data with rationale.
- `human` is the default species; other species (elf, catgirl, mermaid, demon,
  angel, dragon_girl, android, vampire, oni, kitsune, goblin, orc…) MUST be explicit.
- A species MAY be **marked by physical attributes** (e.g. `catgirl` implies
  `animal_ears`); such markers live in the attribute/body ontologies. Species itself
  is the identity-level kind, MUST NOT be reduced to those markers, and MUST NOT be
  hardcoded in application source.

## 3. Character and franchise (open registries)

- **character** entries reference a **franchise** and a **primary species**.
- **franchise** entries are works/copyrights.
- These are **open registries** (thousands of entries); this ontology defines the
  structure and seeds a representative sample. The full catalog is populated from
  data (the reconciliation tag-classification), not enumerated here.

### 3.1 The `character_(franchise)` signal (reconciliation aid)

- Legacy legacy encodes many characters as `character_(franchise)` (e.g.
  `makima_(chainsaw_man)`, `mash_kyrielight_(fate)`): the parenthetical is the
  franchise.
- **Caveat (measured):** the `_(qualifier)` convention is **domain-general**, not
  character-only — e.g. `piledriver_(sex)`, `shrug_(clothing)`, `star_(symbol)`,
  `shower_(place)`, `photo_(medium)`. Classification MUST therefore distinguish a
  *franchise* qualifier from sex/clothing/scene/medium qualifiers before treating a
  tag as `character_(franchise)`.

## 4. Identity modifiers

- The ontology MUST define identity **modifiers** that alter how an identity applies:
  `cosplay` (an entity portraying a different character), `genderswap`,
  `humanization` (a non-human franchise element depicted as a person),
  `alternate_costume`. Modifiers are data (`identity_modifiers.tsv`).

## 5. Observation and resolution

- Species, character, and franchise are asserted by **observations** (ADR-0001); a
  correction is a new observation; the Resolver derives the current identity.
- A scene MAY assert a character identity with **no** confirmed franchise (unknown
  work); franchise is optional on an observation.

## 6. Seed

Baseline ([`/database/seed/identity-ontology/`](../../database/seed/identity-ontology/)):
`species.tsv`, `franchises.tsv` (sample), `characters.tsv` (sample, each →
franchise + primary species), `identity_modifiers.tsv`. Grounded in real legacy tags.

## 7. Conformance / out of scope

Conforms if species/character/franchise have stable ids, characters reference an
existing franchise + species, and nothing is hardcoded in application source. The
full character/franchise catalog, canonical-trait references, and cross-work
identity linking are follow-up.
