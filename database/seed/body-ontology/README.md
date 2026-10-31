# Body Ontology Seed

- **Status:** Draft
- **Version:** 0.2.0
- **Scope:** Seed dataset for the Kura 1.0 body ontology providing **full anatomical
  coverage** of the human figure (a superset of the KTS-HANDOFF-001 §7 baseline),
  per [`ontology/body/SPECIFICATION.md`](../../../ontology/body/SPECIFICATION.md).

## Format

- Files are **TSV** (tab-separated, UTF-8) — relational and **JSON-free**, one file
  per logical relation, so they map directly to rows once schema DDL exists.
- This is **authoring / transport data** (ADR-0002 transport boundary). It MUST NOT
  be treated as canonical DB persistence until loaded through reviewed DDL. No
  production DDL is added here (KTS-HANDOFF-001 §9, §12).
- All terms are modelled neutrally as anatomical vocabulary (ADR-0003).

## Files

| File | Rows | Describes |
|---|---|---|
| `region_classes.tsv` | 9 | region classes (fixed by handoff §7) |
| `regions.tsv` | 117 | regions: slug, display, parent, laterality, primary class |
| `region_class_map.tsv` | 126 | region → region-class (many-to-many) |
| `keypoints.tsv` | 19 | `kura-skeleton-v0` keypoints (incl. ears) |
| `region_keypoint_map.tsv` | 32 | region → keypoint with role (`is` / `anchors`) |
| `applicability.tsv` | 14 | region/class → domain applicability (attribute/relation/pose) |
| `annotation_units.tsv` | 6 | annotation-unit vocabulary |
| `generator_edit_units.tsv` | 4 | generator-edit-unit vocabulary |
| `region_generator_edit_map.tsv` | 15 | region/class → default generator-edit unit |

## Coverage

Full human-figure coverage across: head & face (incl. eye/ear/nose/mouth
sub-structures and facial hair), neck, torso (chest/breast, abdomen, flank), back
(incl. spine/scapula landmarks), pelvis/buttocks/groin, external genitalia and
perianal region (neutral anatomical terms, both sexes), upper limb (down to
fingers/nails), and lower limb (down to toes/nails). Skeletal landmarks and joints
are included to drive geometry and the pose keypoint scheme.

## Conventions

- **Stable id:** `kura:body/<slug>`; lateralized instance `kura:body/<slug>#<left|right>`
  (SPECIFICATION §2). The TSV carries `slug`; the string id is derived.
- **laterality ∈** `{none, midline, bilateral}`. A `bilateral` base expands to
  linked `#left` / `#right` instances (SPECIFICATION §4) — instances are derived,
  not listed as separate base rows.
- **Bilateral keypoint maps** carry a `laterality` column (`left`/`right`/`na`).

## Integrity (verified at authoring time)

- Hierarchy is acyclic with the single root `body`; no duplicate slugs.
- Every `parent_slug`, class, region, and keypoint reference resolves.
- Every region maps to at least one region class; each region's `primary_class`
  appears in `region_class_map`.
- `applicability` and `region_generator_edit_map` keys resolve to a region or class.

## Follow-up

Dense hand/face keypoint schemes, per-term generator prompt strings, and the
loading DDL are follow-up work (KTS-HANDOFF-001 §13).
