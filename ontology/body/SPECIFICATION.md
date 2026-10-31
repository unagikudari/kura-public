# Kura 1.0 — Body Ontology Specification

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Formal specification of the Kura 1.0 body-region ontology: stable
  identifiers, hierarchy, laterality, region classes, applicability rules,
  keypoint mappings, annotation units, and generator-edit units.
- **Source assignment:** [KTS-HANDOFF-001](../../docs/standard/KTS-HANDOFF-001-node-handoff-specification.md) §13.
- **Governing volume/ADR:** [Volume 3 — Ontology](../../docs/standard/volume-03-ontology.md), [ADR-0003](../../docs/adr/ADR-0003-normalized-body-regions.md).

This document is normative about **structure and rules**. It does not add
production DDL (KTS-HANDOFF-001 §9); the seed dataset under
[`/database/seed/body-ontology/`](../../database/seed/body-ontology/) is authored in
a relational, JSON-free tabular format (TSV) ready to load once the schema is
reconciled. Body regions are modelled neutrally as anatomical terms (ADR-0003).

## 1. Model overview

A **body region** is a normalized ontology entry that, by default, belongs to a
**person entity** (ADR-0003). The ontology is a set of regions plus five kinds of
data attached to them:

1. **Region classes** — what kind of surface/structure a region is.
2. **Hierarchy** — part-of relationships between regions.
3. **Laterality** — how left/right/midline is encoded.
4. **Keypoint mappings** — how regions relate to a pose skeleton.
5. **Applicability, annotation units, generator-edit units** — how a region may be
   annotated, reasoned about, and edited.

All of the above MUST be data-driven; none MUST be hardcoded in application source.

## 2. Stable identifiers

- Every region MUST have a **stable identifier** that is independent of its display
  name. Renaming a region MUST NOT change its identifier.
- The canonical stable identifier MUST be a string in the reserved namespace
  `kura:body/<slug>`, where `<slug>` is a lowercase, underscore-separated,
  laterality-free base slug (e.g. `kura:body/nipple`).
- A concrete lateralized instance MUST be identified by suffixing the laterality:
  `kura:body/<slug>#<laterality>` where `<laterality> ∈ {left, right}` (e.g.
  `kura:body/nipple#left`). Non-lateral regions MUST NOT carry a suffix.
- A relational surrogate key (integer) MAY be assigned per row for storage, but the
  string identifier above MUST be the cross-system stable reference.
- Deprecated regions MUST remain resolvable by identifier; they MUST NOT be deleted
  (ADR-0001/ADR-0003 spirit). A deprecated region MUST carry a status marking it so.

## 3. Hierarchy

- Regions MUST form a **part-of** hierarchy that MUST be acyclic (a forest with a
  single conventional root `body`).
- Each region MUST declare at most one parent via `parent_slug`; the root's parent
  MUST be empty.
- The part-of relation MUST be interpreted as "is anatomically part of", not "is
  visually near".
- Depth is unbounded but SHOULD stay shallow enough to remain reviewable
  (`body → torso → breast → nipple` is a representative chain).

## 4. Laterality

- Laterality MUST be encoded explicitly (Volume 3 §4); left/right MUST NOT be
  modelled as two unrelated base terms.
- Each **base region** MUST declare a `laterality` value from:
  `none` (single, off-axis nothing), `midline` (single, on the body midline, e.g.
  `nose`, `navel`), or `bilateral` (occurs as a left/right pair).
- A `bilateral` base region MUST expand into exactly two concrete instances
  (`#left`, `#right`) sharing the base identifier; the pair MUST be linked to their
  base region.
- Instances MUST inherit region classes, parent, keypoint mappings, and
  applicability from their base unless the seed overrides them explicitly.
- Consumers MUST be able to query either the base region (laterality-agnostic) or a
  specific instance.

## 5. Region classes

- Each region MUST map to **one or more** region classes.
- The initial region classes are fixed by KTS-HANDOFF-001 §7:

  | Class | Meaning |
  |---|---|
  | `surface_skin` | External keratinized/normal skin surface. |
  | `surface_mucosa` | Mucosal surface. |
  | `hair` | Hair-bearing region / hair as a region. |
  | `eye` | Ocular structures. |
  | `mouth` | Oral region/structures. |
  | `external_genital` | External genital anatomy. |
  | `perianal` | Perianal region. |
  | `bone_landmark` | A skeletal landmark used for geometry/pose. |
  | `joint` | An articulation used for geometry/pose. |

- Region classes drive default applicability and default generator-edit behaviour
  (a `surface_*` region is inpaintable; a `joint`/`bone_landmark` is a geometry
  reference, not a paintable surface).
- New region classes MUST be added as data with an accompanying rationale; they
  MUST NOT be introduced ad hoc in application code.

## 6. Keypoint mappings

- The ontology MUST define a **keypoint scheme** (a named skeleton) and MAY map
  regions — primarily `joint` and `bone_landmark` regions — to keypoints in it.
- The initial scheme is `kura-skeleton-v0`, a neutral whole-body skeleton
  (see the seed `keypoints.tsv`). It exists to drive pose/ControlNet generation
  (Volume 8) and pose validation (Volume 7).
- A region-to-keypoint mapping MUST record the region, the keypoint, and the
  mapping role (`is` for a landmark that *is* the keypoint, `anchors` for a region
  positioned by a keypoint).
- Keypoint schemes MUST be versioned; a generator adapter MUST declare which scheme
  version it consumes.

## 7. Applicability rules

- Applicability MUST express, as data, which **domains** (attribute, relation,
  pose) may attach to a region or a region class.
- An applicability rule MUST be keyed by either a region `slug` or a region `class`
  (class-level rules apply to every region of that class unless a region-level rule
  overrides).
- A rule MUST name the target `domain` (`attribute` | `relation` | `pose`) and MAY
  carry a note; concrete attribute/relation/pose *terms* live in their own
  ontologies (Volume 3) and MUST NOT be duplicated here.
- Example intent (seeded): `surface_mucosa` regions are applicable to
  moisture/state attributes; `joint` regions are applicable to pose-angle
  attributes; `external_genital` regions are applicable to contact relations.

## 8. Annotation units

- An **annotation unit** is the granularity at which a human confirms or corrects a
  region observation. Annotation MUST be comparison-based (Volume 4 §5), captured as
  new observations (ADR-0001).
- The ontology MUST define an enumerated set of annotation-unit types. The initial
  set (see `annotation_units.tsv`):

  | Unit | Meaning |
  |---|---|
  | `presence` | Region present / absent in the scene. |
  | `visibility` | Region visible / occluded. |
  | `laterality_confirm` | Confirm which instance (left/right) is meant. |
  | `region_bbox` | Bounding box localization of the region. |
  | `region_mask` | Pixel mask localization of the region. |
  | `class_correction` | Correct the region-class assignment. |

- A region or region class MAY restrict which annotation units apply to it via
  applicability (§7); absent a restriction, `presence` and `visibility` MUST apply.

## 9. Generator-edit units

- A **generator-edit unit** is the granularity at which a region can be targeted for
  generation or editing, and it MUST map to Volume 8's instruction set as a derived
  projection (INV-7).
- The ontology MUST define an enumerated set of generator-edit-unit types. The
  initial set (see `generator_edit_units.tsv`):

  | Unit | Meaning | Volume 8 target |
  |---|---|---|
  | `prompt_fragment` | Region contributes semantic prompt terms. | positive/negative terms |
  | `regional_mask` | Region defines a spatial conditioning mask. | regional conditions |
  | `inpaint_region` | Region is an inpaint/edit target. | regional conditions |
  | `pose_keypoint` | Region drives a ControlNet keypoint. | spatial conditioning |

- `surface_*` regions SHOULD default to `prompt_fragment` + `inpaint_region`;
  `joint`/`bone_landmark` regions SHOULD default to `pose_keypoint`. Defaults MUST
  be overridable by data.

## 10. Seed dataset

The seed under [`/database/seed/body-ontology/`](../../database/seed/body-ontology/)
provides **full anatomical coverage** of the human figure (117 regions) in this
model, a superset of the KTS-HANDOFF-001 §7 baseline. It is TSV (relational,
JSON-free) and is authoring/transport data pending schema reconciliation; it MUST
NOT be treated as canonical DB persistence until loaded through reviewed DDL. All
terms are modelled neutrally as anatomical vocabulary (ADR-0003).

| File | Rows describe |
|---|---|
| `region_classes.tsv` | the nine region classes |
| `regions.tsv` | base regions: slug, display, parent, laterality, primary class |
| `region_class_map.tsv` | region → region-class (many-to-many) |
| `keypoints.tsv` | `kura-skeleton-v0` keypoints |
| `region_keypoint_map.tsv` | region → keypoint (role) |
| `applicability.tsv` | region/class → domain applicability |
| `annotation_units.tsv` | annotation-unit vocabulary |
| `generator_edit_units.tsv` | generator-edit-unit vocabulary |
| `region_generator_edit_map.tsv` | region/class → generator-edit unit (default) |

## 11. Conformance

An implementation conforms to this specification if:

- every region has a stable identifier per §2 and at least one region class per §5;
- the hierarchy is acyclic with root `body` (§3);
- laterality is explicit and bilateral bases expand to linked left/right instances (§4);
- keypoint mappings reference a versioned scheme (§6);
- applicability, annotation units, and generator-edit units are data-driven (§7–§9);
- no ontology term list is hardcoded in application source (Volume 3, §9 of handoff).

## 12. Out of scope

Finalized keypoint geometry (dense hand/face keypoint schemes beyond
`kura-skeleton-v0`), per-term generator prompt strings, and the loading DDL are
follow-up work. Full anatomical **region** coverage is provided by the seed (§10);
deeper sub-region detail MAY be added later as data without changing this model.
