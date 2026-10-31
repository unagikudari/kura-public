# Body Ontology

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Normalized body-region ontology for Kura 1.0. Structure and baseline
  only; the full dataset is follow-up work (KTS-HANDOFF-001 §13). See ADR-0003.

## 1. Requirements

- Body regions MUST be normalized ontology entries with stable identifiers.
- Regions MUST be modelled neutrally as anatomical terms.
- By default a region belongs to a **person entity**; it MUST NOT be a separate
  scene entity unless separate relation/geometry semantics are explicitly required.
- Region terms MUST NOT be hardcoded in application source; they are data.

## 2. Baseline region terms (neutral anatomical vocabulary)

These are anatomical terms modelled neutrally per KTS-HANDOFF-001 §7:

- `body, head, face, forehead, cheek, nose, eye, iris, pupil, mouth, neck`
- `torso, left breast, right breast, left areola, right areola, left nipple, right nipple`
- `pelvis, pubic region, external genitalia, penis, scrotum, vulva, perineum, anus`
- `left arm, right arm, left hand, right hand, left leg, right leg, left foot, right foot`

## 3. Region classes

Initial region classes:

`surface_skin`, `surface_mucosa`, `hair`, `eye`, `mouth`, `external_genital`,
`perianal`, `bone_landmark`, `joint`.

## 4. Formal specification and seed

The formal body-ontology specification — stable identifiers, hierarchy, laterality,
region classes, applicability rules, keypoint mappings, annotation units, and
generator-edit units — is in **[SPECIFICATION.md](./SPECIFICATION.md)**
(KTS-HANDOFF-001 §13).

The seed dataset instantiating the §7 baseline in that model lives under
**[`/database/seed/body-ontology/`](../../database/seed/body-ontology/)** (TSV,
relational, JSON-free; authoring data pending schema reconciliation — no production
DDL, KTS-HANDOFF-001 §9).
