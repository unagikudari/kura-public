# Kura 1.0 — Pose Ontology Specification

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Pose — the configuration of a **person entity** — as data-driven,
  composable descriptors bridged to the body-ontology keypoint scheme
  (`kura-skeleton-v0`). Closes the `pose` gap from
  [database reconciliation](../../database/RECONCILIATION.md) §4/§6.
- **Source:** [KTS-HANDOFF-001](../../docs/standard/KTS-HANDOFF-001-node-handoff-specification.md) §13, [Volume 3](../../docs/standard/volume-03-ontology.md), body-ontology [SPECIFICATION](../body/SPECIFICATION.md) §6 (keypoints), [Volume 7](../../docs/standard/volume-07-validation.md) (pose validation), [Volume 8](../../docs/standard/volume-08-generator-mapping.md) (ControlNet).

No production DDL (KTS-HANDOFF-001 §9). Seed under
[`/database/seed/pose-ontology/`](../../database/seed/pose-ontology/) is TSV,
JSON-free authoring data. Terms are neutral.

## 1. Model

- Poses MUST be data-driven ontology entries with stable identifiers
  `kura:pose/<slug>`; renaming MUST NOT change the identifier.
- A pose describes a **person entity** (INV-5); it MUST be expressible independently
  of any single asset, so the same pose can describe a source scene and a generation
  target.
- A pose MUST NOT encode inter-entity configuration. A configuration involving two
  entities (e.g. one person sitting on another) is a **relation**
  ([relation ontology](../relations/SPECIFICATION.md)) that MAY co-occur with
  per-entity poses.

## 2. Scope and composability

- Every pose MUST declare a **scope**: `whole_body`, `upper_body`, `lower_body`,
  `legs`, `arms`, `hands`, or `head`.
- Poses of different scopes **compose**: a person MAY simultaneously carry one
  `whole_body` posture plus scoped descriptors (e.g. `sitting` + `spread_legs` +
  `arms_up`). Two poses of the **same** scope SHOULD be mutually exclusive on one
  entity at one time.

## 3. Categories

| Category | Meaning |
|---|---|
| `posture` | Whole-body posture (standing, sitting, lying, kneeling…). |
| `leg_configuration` | Configuration of the legs. |
| `arm_configuration` | Configuration of the arms. |
| `gesture` | A hand/limb gesture. |
| `self_contact` | The person's own limb/region contacting another own region. |

## 4. Keypoint bridge (the core link)

- A pose MUST declare which `kura-skeleton-v0` **keypoints it constrains**
  (`pose_keypoints.tsv`), referencing the body-ontology keypoint scheme.
- This is the bridge that makes a pose usable: the constrained keypoints are what a
  ControlNet/OpenPose generator conditions on (Volume 8) and what pose-plausibility
  validation checks (Volume 7).
- The **exact geometry** (keypoint coordinates / joint angles) is deliberately **not**
  fixed here — that is the "finalized keypoint geometry" follow-up. This spec fixes
  *which* keypoints a pose governs; a later revision adds the numeric constraints.
- A pose MAY additionally reference the **body regions** it involves
  (`pose_regions.tsv`), for validation and generator region targeting.

## 5. Observation and resolution

- A pose on an entity is asserted by **observations** (ADR-0001); a correction is a
  new observation; the Resolver derives the current pose set (Volume 4).
- Because poses compose (§2), the resolved pose of an entity is a **set** of scoped
  descriptors, not a single value.

## 6. Generator and validation mapping

- Poses map to generation as derived projections (INV-7): the constrained keypoints
  drive a ControlNet pose skeleton (Volume 8); a pose MAY declare a prompt fragment.
- Volume 7 pose validation checks joint/laterality plausibility against the
  constrained keypoints of the resolved pose set.

## 7. Seed

Baseline ([`/database/seed/pose-ontology/`](../../database/seed/pose-ontology/)):
`pose_scopes.tsv`, `pose_categories.tsv`, `poses.tsv`,
`pose_keypoints.tsv` (→ `kura-skeleton-v0`), `pose_regions.tsv` (→ body regions).
Grounded in real legacy pose tags (single-person poses; inter-entity tags routed to
relations).

## 8. Conformance / out of scope

Conforms if poses have stable ids, a declared scope + category, and constrained
keypoints resolving to `kura-skeleton-v0`, with nothing hardcoded in application
source. Numeric keypoint geometry, dense hand/face pose schemes, and full pose
coverage are follow-up.
