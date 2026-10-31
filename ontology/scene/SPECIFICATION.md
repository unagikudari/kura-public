# Kura 1.0 — Scene Ontology Specification

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Scene-level ontology: camera, perspective, composition/framing, and
  setting descriptors — the scene properties that are independent of any single
  asset and that Volume 7 validates and Volume 8 maps to generation.
- **Source:** [KTS-HANDOFF-001](../../docs/standard/KTS-HANDOFF-001-node-handoff-specification.md) §13, [Volume 3](../../docs/standard/volume-03-ontology.md), [Volume 7](../../docs/standard/volume-07-validation.md).

No production DDL (KTS-HANDOFF-001 §9). The seed under
[`/database/seed/scene-ontology/`](../../database/seed/scene-ontology/) is TSV,
JSON-free authoring data.

## 1. Model

- Scene descriptors MUST be data-driven ontology entries with **stable identifiers**
  `kura:scene/<slug>`; renaming MUST NOT change the identifier (Volume 3 §3).
- Descriptors are organized into **facets** (a small, fixed set of scene axes). Each
  descriptor belongs to exactly one facet.
- Scene descriptors MUST be expressible **independently of any single asset**, so the
  same scene can back a source, a generation target, and a generated output
  (Volume 1 §2).
- Camera and perspective MUST be first-class scene facets (Volume 7 §4), so a target's
  camera/perspective can be compared against the observed camera/perspective of a
  generated asset (Volume 7 §5).

## 2. Facets

| Facet | Meaning |
|---|---|
| `camera_angle` | Vertical/horizontal viewpoint relative to the subject. |
| `shot_type` | Framing distance / how much of the subject is in frame. |
| `perspective` | Projection/perspective character of the scene. |
| `composition` | Arrangement of subject within the frame. |
| `orientation` | Aspect/orientation of the frame. |
| `setting` | Environment/location character. |

- New facets MUST be added as data with rationale; they MUST NOT be introduced ad hoc
  in application source.

## 3. Descriptor requirements

- Each descriptor MUST declare its `facet`, a stable `slug`, and a neutral display
  name.
- Descriptors within a facet SHOULD be mutually comparable; a scene MAY carry at most
  one primary descriptor per facet (e.g. one `camera_angle`) but MAY carry secondary
  qualifiers where the facet allows.
- Continuous camera/perspective properties (e.g. pitch angle, focal-length-implied
  field of view) MAY additionally be represented as scalar estimates with intervals
  (Volume 1 §6); the categorical descriptor is the coarse, human-facing label.

## 4. Camera and perspective (first-class)

- `camera_angle` and `shot_type` descriptors MUST be sufficient to state a generation
  **target** camera and to classify an **observed** camera for comparison (Volume 7).
- A `camera_angle` descriptor MAY map to a scalar pitch/yaw hint for generator use
  (Volume 8), recorded as a derived projection, not canonical truth.
- Perspective descriptors MUST capture at least the common projection characters
  (eye-level neutral, foreshortening, wide-angle distortion) needed by validation.

## 5. Relationship to poses and entities

- Scene descriptors describe the **scene/camera**, not the person. Person
  configuration is the **pose** domain (body ontology keypoints + a pose ontology);
  a scene's `camera_angle` and a person's pose are independent and MUST be modelled
  separately.
- Gaze (Volume 7) is an entity/eye property, not a scene facet; it MAY be validated
  against `camera_angle` (e.g. "looking at camera").

## 6. Generator mapping

- Scene descriptors MAY declare generator mappings (prompt fragments, camera/ControlNet
  hints) as derived projections (Volume 8, INV-7).

## 7. Seed

The seed instantiates a baseline vocabulary per facet
([`/database/seed/scene-ontology/`](../../database/seed/scene-ontology/)):
`scene_facets.tsv`, `scene_descriptors.tsv`, `scene_descriptor_prompt_map.tsv`
(derived-projection generator hints).

## 8. Conformance / out of scope

Conforms if descriptors have stable ids, each belongs to one declared facet, camera
and perspective are represented, and nothing is hardcoded in application source.
Finalized scalar camera parameterization and per-descriptor prompt strings are
follow-up.
