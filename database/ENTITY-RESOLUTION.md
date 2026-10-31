# Kura 1.0 — Entity Resolution Semantics

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** How legacy's **per-asset flat tags** become Kura 1.0's **per-entity
  observations** — entity instantiation, gender presentation vs anatomy, and the
  conservative rules for attaching an ambiguous flat tag to an entity. The last
  design piece before production DDL.
- **Source:** [RECONCILIATION.md](./RECONCILIATION.md) §5–§6, [Volume 4](../docs/standard/volume-04-processing.md) (Resolver).

No production DDL (KTS-HANDOFF-001 §9). Seed under
[`/database/seed/entity-resolution/`](./seed/entity-resolution/) is TSV, JSON-free.
Terms are neutral.

## 1. The problem

legacy tags attach to an **asset** (image), not to entities. Kura 1.0 attaches body
regions, attributes, and poses to **entities**. A flat tag on a multi-person image
does not say **which** person it describes: `2girls` + `long_hair` + `short_hair`
tells us two hairstyles exist, not who has which. Entity resolution MUST bridge this
**without inventing** the missing (entity → observation) links.

## 2. Entity kinds and gender presentation

- Scene entities have a **kind**: `person` or `object` (objects: object ontology).
- A `person` entity carries a **gender presentation** — `female`, `male`, `other`,
  `androgynous`, `ambiguous` — asserted by count/marker tags (§3).
- **Gender presentation is identity-level and MUST be kept separate from anatomy.**
  Anatomy (penis, vulva…) is a **body-region observation** (body ontology) that MAY
  or MAY NOT align with presentation:
  - `otoko_no_ko` ⇒ a `male`-presentation entity with a feminine depiction.
  - `futanari` ⇒ an entity carrying **both** male and female anatomy regions.
  Consumers MUST NOT infer anatomy from presentation, nor presentation from anatomy.

## 3. Entity-count vocabulary → instantiation

- `N girl(s)` ⇒ instantiate **N** `person` entities, presentation `female`.
- `N boy(s)` ⇒ **N** `person`, presentation `male`.
- `N other(s)` ⇒ **N** `person`, presentation `other`.
- `multiple_<x>` ⇒ count is a **lower bound** (`>=2`), not exact.
- `6+girls` ⇒ `>=6`.
- The scene's person count is the **sum** across presentation categories
  (`1girl` + `1boy` ⇒ 2 entities). Counts are asserted as observations; the Resolver
  reconciles them (a later, contradicting count is new evidence, ADR-0001).
- `solo` ⇒ **exactly one** person entity in the scene.
- `solo_focus` ⇒ one **primary** entity; others MAY exist (focus, not exclusivity).
- `no_humans` ⇒ **zero** person entities (object/scene only).
- **`multiple_penises` and similar are ANATOMY counts, NOT entity counts** — they MUST
  NOT instantiate entities; they inform male-anatomy region observations.

## 4. Attachment scopes and the ambiguity rule (core)

An observation derived from a flat tag attaches at one of three **scopes**:

| Scope | When | Meaning |
|---|---|---|
| `entity` | the target entity is unambiguous | attached to that entity |
| `scene` | the tag is inherently scene-level | attached to the scene (camera, setting, rendering, entity counts) |
| `unresolved` | person-scoped but the entity is ambiguous | attached to the scene with an **ambiguity marker**, pending disambiguation |

Rules:

- **Single person entity** (`solo`, or exactly one person resolved): every
  person-scoped observation (body region, attribute, pose) attaches at `entity`
  scope to that sole entity — unambiguous.
- **Multiple person entities**: a person-scoped flat tag is **ambiguous**. It MUST
  attach at `unresolved` scope with the candidate entity set, and MUST NOT be guessed
  onto a specific entity. Disambiguation is a **later observation** (human or model
  spatial localization), never a fabrication by the Resolver.
- Cardinality hints MAY narrow ambiguity but MUST NOT force a choice: `2girls` with
  one `short_hair` and one `long_hair` establishes that the two hairstyles are
  distributed across the two female entities, but NOT which entity holds which.
- **Non-fabrication is binding**: the Resolver MUST NOT emit an (entity, attribute)
  link that the observations do not support. An `unresolved` observation is a truthful
  outcome; a guessed `entity` link is a defect.

### 4.1 Where the candidate set lands (migration 0003)

"Attach with the candidate entity set" was stated as a rule but had **no table to land
in** until migration 0003. It now lands in:

```
unresolved_subject_candidate (resolver_run_id, observation_id, entity_id, rank, exclusivity)
```

- `rank` — 1 = most likely. Ordering is part of the claim; dropping it turns a ranked
  shortlist into an unordered "one of these".
- `exclusivity` — `exclusive` (exactly one candidate holds the fact) vs `inclusive`
  (several may). The `2girls` / `short_hair` + `long_hair` case in §4 is **exclusive**:
  the two hairstyles are distributed one-each. Without this column that distribution —
  the actual information the tags carry — cannot be written down.

The row is keyed by `resolver_run_id` because candidates are stated against a specific
entity set, and entity ids only mean anything within their run (§7).

### 4.2 Human annotation names subjects before entities exist

Entities are the Resolver's output, so an annotator cannot reference them. Annotation
records `observation.subject_ref` (`person_1`) instead, and `obs_relation` carries
`subject_ref` / `subject_region_slug` / `object_ref` / `object_region_slug`
(migration 0004). The CHECK permits **at most one participant form per role** and
**zero** participants, so a genuinely one-sided relation ("a hand is grasping her
breast", owner off-frame) is expressible without inventing the other party — the same
rule as §4, applied to relations.

## 5. Relations across entities

- Relation tags (`hetero`, `sex`, `futa_with_male`, `hand_on_another's_head`) assert
  a relation **between** entities. Participants are resolved only as far as the
  evidence allows:
  - `hetero` on a `1girl` + `1boy` scene ⇒ a relation between the female and the male
    entity (participants resolvable by presentation).
  - The same tag on `2girls` + `2boys` leaves participant identity `unresolved`.
- Self-relations (`hand_on_own_hip`) resolve within a single entity.

## 6. Objects

- Object tags instantiate/attach `object` entities (object ontology). `wearing`
  relations to a person follow the same ambiguity rule: unambiguous in `solo`,
  `unresolved` in multi-person scenes.
- `no_humans` scenes carry only object/environment entities.

## 7. Resolver policy

- Entity resolution is a **Resolver** responsibility (Volume 4): deterministic given
  (observation set, Resolver version), reproducible, and recorded.
- The resolved scene's entity set and each observation's attachment scope are part of
  the resolved state; re-resolution on new (disambiguating) observations produces a
  new resolved version, never a mutation (ADR-0001).

### 7.1 Entities are owned by a run (migration 0002)

`entity` carries `resolver_run_id` and `UNIQUE (id, resolver_run_id)`. Everything that
points at an entity — `resolved_entity_binding`, `entity_region`, and **both** the
subject and object sides of `obs_relation` — references it by the composite key
`(entity_id, resolver_run_id)`.

The consequence is worth stating plainly: **an entity id alone is not a reference.**
Run 1's "entity 7" and run 2's "entity 7" are different claims about the scene, and the
composite FK makes mixing them a database error rather than a subtle wrong answer. A
binding table by itself would not have prevented this — the ids would still have joined.

Attachment is recorded in `resolved_entity_binding (resolver_run_id, observation_id,
entity_id)`; an observation with no row there is either scene-scoped or unresolved
(§4.1), and those two cases are distinguished by `observation.subject_scope`.

## 8. Seed

[`/database/seed/entity-resolution/`](./seed/entity-resolution/):
`entity_kinds.tsv`, `gender_presentations.tsv`, `entity_count_tags.tsv`
(count/marker tag → instantiation rule), `attachment_scopes.tsv`.

## 9. Conformance / out of scope

Conforms if entity instantiation follows §3, presentation is kept separate from
anatomy (§2), person-scoped tags in multi-entity scenes resolve to `unresolved` rather
than a guessed entity (§4), the candidate set is **written** with rank and exclusivity
(§4.1), and entity references are run-scoped (§7.1). A run that resolves everything and
leaves `unresolved_subject_candidate` empty on a multi-person scene is not conformant —
it has guessed. Automated disambiguation (spatial localization
models) and the full count-tag catalog are follow-up.
