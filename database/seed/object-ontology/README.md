# Object Ontology Seed

- **Status:** Draft — **Version:** 0.1.0
- **Scope:** Baseline object vocabulary per
  [`ontology/object/SPECIFICATION.md`](../../../ontology/object/SPECIFICATION.md),
  grounded in real legacy clothing/prop tags. TSV, JSON-free; no production DDL
  (KTS-HANDOFF-001 §9).

| File | Rows | Describes |
|---|---|---|
| `object_categories.tsv` | 5 | clothing / accessory / prop / furniture / environment |
| `clothing_slots.tsv` | 11 | headwear … full_body |
| `objects.tsv` | 38 | object: slug, category, slot, participation |
| `object_coverage.tsv` | 65 | clothing → covered **body-ontology** regions |
| `object_states.tsv` | 8 | worn / removed / open / displaced / lifted / torn / transparent |
| `object_relations.tsv` | 4 | wearing / holding / using / present_in_scene (extend relation ontology) |

## Conventions & integrity

- **Stable id:** `kura:object/<slug>`. Clothing declares a `slot`; coverage
  `covered_key` resolves to real **body-ontology** regions/classes (verified at
  authoring time — 65/65 OK).
- Object **states** modify effective coverage (`removed` ⇒ none, `transparent` ⇒
  covered-but-visible, `displaced`/`lifted` ⇒ partial); flat legacy state tags
  (`no_panties`, `open_shirt`, `torn_pantyhose`, `panty_peek`) reconcile to
  `(object, state)`.

## Follow-up

Full clothing/prop coverage, a material ontology, and folding the participation
relations into the relation ontology are follow-up.
