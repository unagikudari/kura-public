# Kura 1.0 Standard — Volume 3: Ontology Specification

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Body, object, scene, pose, attribute, and relation ontologies;
  hierarchy, laterality, applicability, stable identifiers, and generator mappings.

## 1. Scope

This volume defines the ontology that drives Kura 1.0 behaviour. Ontology content
MUST be data-driven (stored, versioned data), and MUST NOT be hardcoded in
application source (KTS-HANDOFF-001 §9). This foundation defines structure and
baselines; the full ontology dataset is follow-up work (§13).

## 2. Ontology domains

The ontology MUST cover, as distinct but linked domains:

- **Body** — anatomical regions (see `/ontology/body`).
- **Object** — non-body physical entities in a scene.
- **Scene** — scene-level descriptors and composition.
- **Pose** — configuration of a person entity (see §5).
- **Attribute** — properties attaching to entities (see `/ontology/attributes`).
- **Relation** — relationships between entities (see `/ontology/relations`).

## 3. Stable identifiers

- Every ontology term MUST have a stable, opaque identifier that is independent of
  its display name.
- Renaming a term MUST NOT change its identifier.
- Deprecated terms MUST remain resolvable; they MUST NOT be deleted.

## 4. Hierarchy, laterality, applicability

- Terms MAY form a hierarchy (parent/child); the hierarchy MUST be acyclic.
- Terms with laterality (e.g. left/right) MUST encode laterality explicitly rather
  than as separate unrelated terms.
- Applicability rules (which attributes/relations/poses apply to which entity
  kinds) MUST be expressed as data, not code branches.

## 5. Pose ontology

- Pose MUST be modelled as a first-class ontology domain attaching to person
  entities (INV-5).
- A pose MUST be expressible independently of any single image, so the same pose
  can describe a source scene and a generation target.
- Pose terms MUST carry stable identifiers and MAY map to keypoint/skeleton
  representations for generator use (Volume 8).

## 6. Generator mappings

- Ontology terms MAY declare generator-facing mappings (prompt fragments, LoRA,
  ControlNet hints); these mappings MUST be derived projections (INV-7), not
  canonical truth.

## 7. Directory layout

| Path | Content |
|---|---|
| `/ontology/body` | Body-region ontology and region classes. |
| `/ontology/attributes` | Attribute ontology. |
| `/ontology/relations` | Relation ontology. |
| `/ontology/scene` | Scene-level ontology. |

## 8. Out of scope

The concrete term dataset, keypoint mappings, and applicability tables are the
next assignment (KTS-HANDOFF-001 §13) and MUST NOT be finalized here.
