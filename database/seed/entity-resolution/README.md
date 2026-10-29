# Entity Resolution Seed

- **Status:** Draft — **Version:** 0.1.0
- **Scope:** Entity-kind, gender-presentation, attachment-scope, and count/marker-tag
  instantiation rules supporting [`database/ENTITY-RESOLUTION.md`](../../ENTITY-RESOLUTION.md).
  TSV, JSON-free; no production DDL (KTS-HANDOFF-001 §9).

| File | Rows | Describes |
|---|---|---|
| `entity_kinds.tsv` | 2 | person / object |
| `gender_presentations.tsv` | 5 | female / male / other / androgynous / ambiguous |
| `attachment_scopes.tsv` | 3 | entity / scene / **unresolved** |
| `entity_count_tags.tsv` | 25 | count/marker tag → (kind, presentation, count_spec, is_entity_count) |

## Key rules (measured)

- `Ngirl(s)`/`Nboy(s)`/`Nother(s)` → N `person` entities of that presentation; scene
  person count is the **sum** across presentations.
- `solo`=exactly 1, `solo_focus`=1 primary (non-exclusive), `no_humans`=0 persons
  (flagged `is_entity_count=meta`).
- **`multiple_penises` is an ANATOMY count, not an entity count** (`is_entity_count=no`)
  — it must not instantiate entities.
- `futanari`/`otoko_no_ko`/`androgynous` are **presentation/anatomy markers**, not
  counts. Gender **presentation** (identity) is kept separate from **anatomy** (body
  observations).

## The core non-fabrication rule

In a **multi-person** scene a person-scoped flat tag is ambiguous → it resolves to
`unresolved` scope with the candidate set, **never guessed onto a specific entity**.
Only `solo`/single-entity scenes give unambiguous `entity`-scope attachment.
Disambiguation is a later observation, not a Resolver guess.

## Follow-up

Automated disambiguation (spatial-localization models) and the full count-tag catalog
are follow-up.
