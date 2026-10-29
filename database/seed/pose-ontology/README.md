# Pose Ontology Seed

- **Status:** Draft — **Version:** 0.1.0
- **Scope:** Baseline pose vocabulary per
  [`ontology/pose/SPECIFICATION.md`](../../../ontology/pose/SPECIFICATION.md),
  grounded in real legacy single-person pose tags. TSV, JSON-free; no production DDL
  (KTS-HANDOFF-001 §9).

| File | Rows | Describes |
|---|---|---|
| `pose_scopes.tsv` | 7 | whole_body / upper_body / lower_body / legs / arms / hands / head |
| `pose_categories.tsv` | 5 | posture / leg_configuration / arm_configuration / gesture / self_contact |
| `poses.tsv` | 39 | pose: slug, scope, category |
| `pose_keypoints.tsv` | 192 | pose → constrained `kura-skeleton-v0` keypoints |
| `pose_regions.tsv` | 25 | pose → involved **body-ontology** regions |

## Conventions & integrity

- **Stable id:** `kura:pose/<slug>`. Poses **compose** by scope (e.g. `sitting` +
  `spread_legs` + `arms_up`); same-scope poses are mutually exclusive on one entity.
- `pose_keypoints` resolve to `kura-skeleton-v0` (**192/192 OK**); `pose_regions`
  resolve to body-ontology regions (**25/25 OK**); every pose has ≥1 keypoint.
- Inter-entity tags (sitting_on_lap, straddling, sitting_on_person…) are **not**
  poses — they route to the relation ontology.

## Follow-up

Numeric keypoint geometry (coordinates/joint angles), dense hand/face pose schemes,
and full pose coverage are follow-up. This seed fixes *which* keypoints each pose
governs; geometry is a later revision.
