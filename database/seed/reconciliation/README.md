# Reconciliation Seed (legacy → Kura 1.0)

- **Status:** Draft — **Version:** 0.1.0
- **Scope:** The tag-domain taxonomy and a **worked classification of real legacy tags**
  supporting [`database/RECONCILIATION.md`](../../RECONCILIATION.md). TSV, JSON-free;
  no production DDL (KTS-HANDOFF-001 §9).

| File | Describes |
|---|---|
| `tag_domain_taxonomy.tsv` | the 9 target domains a flat legacy tag can split into, and whether each becomes a scene observation |
| `tag_classification_sample.tsv` | 63 **real** legacy tags (measured 2026-07-26 from nodeb `legacy`, top-frequency + random sample) classified into `(domain, ontology_target, note)` |

## What this shows

- legacy's flat tags conflate **9 domains** — a rename cannot reconcile them; each tag
  must be classified and re-expressed (see RECONCILIATION §4).
- `body_region` targets resolve to real **body-ontology** slugs (verified).
- **21 of 63** sample tags hit a `-` gap, honestly surfacing what is still missing:
  - **object ontology** (clothing/props: pantyhose, top_hat, popsicle…) — not yet specified.
  - **character identity** (mash_kyrielight, monster_girl).
  - **pose seed** (spread_legs, knees_to_chest…) — pose domain exists, seed pending.
  - a few **attributes** (erection/penis-state, breast_size) and a scene **rendering**
    facet (monochrome/greyscale) not yet in the ontology seeds.

## Not in scope here

The **full 16,626-tag** classification is LLM-assisted follow-up. This seed defines
the taxonomy and proves the mechanism on a representative sample; it is authoring
data, not canonical persistence, and loads only after the schema DDL exists.
