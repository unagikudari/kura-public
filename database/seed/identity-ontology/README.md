# Identity Ontology Seed

- **Status:** Draft — **Version:** 0.1.0
- **Scope:** Baseline identity vocabulary per
  [`ontology/identity/SPECIFICATION.md`](../../../ontology/identity/SPECIFICATION.md).
  TSV, JSON-free; no production DDL (KTS-HANDOFF-001 §9).

| File | Rows | Describes |
|---|---|---|
| `species.tsv` | 28 | bounded species taxonomy (slug, display, `marked_by` attribute hint) |
| `franchises.tsv` | 11 | representative works/copyrights (open registry) |
| `characters.tsv` | 12 | representative named characters → franchise + primary species |
| `identity_modifiers.tsv` | 4 | cosplay / genderswap / humanization / alternate_costume |

## Conventions & integrity

- **Stable ids:** `kura:species/<slug>`, `kura:character/<slug>`, `kura:franchise/<slug>`.
- Every character's `franchise_slug` ∈ franchises and `primary_species` ∈ species
  (**verified**).
- **`character_(franchise)` signal:** legacy legacy encodes the franchise in the
  parenthetical (makima_(chainsaw_man)); but `_(qualifier)` is domain-general
  (piledriver_(sex), shrug_(clothing), star_(symbol)), so classification must
  confirm the qualifier is a franchise before treating a tag as a character.
- Species `marked_by` names an attribute/body marker (catgirl→animal_ears) but the
  species is the identity-level kind, not the marker.

## Follow-up

The full character/franchise catalog (thousands of entries, from tag
classification), canonical-trait references, and cross-work identity linking are
follow-up. This seed fixes the structure + a representative sample.
