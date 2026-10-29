# Pose Ontology

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Poses — person-entity configuration — as composable descriptors bridged
  to the body-ontology keypoint scheme. Formal spec:
  **[SPECIFICATION.md](./SPECIFICATION.md)**.

## 1. Requirements

- Poses MUST be data-driven ontology entries with stable identifiers
  `kura:pose/<slug>`; not hardcoded in application source.
- A pose describes a **person entity** and MUST be expressible independently of any
  asset. Inter-entity configurations are **relations**, not poses.
- Every pose MUST declare a **scope** (composability) and the `kura-skeleton-v0`
  **keypoints it constrains** (the bridge to ControlNet generation / pose validation).

## 2. Why this exists

The [database reconciliation](../../database/RECONCILIATION.md) flagged pose tags
(spread_legs, knees_to_chest, all_fours…) with no Kura 1.0 target. This ontology is
that target, and it connects the [body ontology](../body/SPECIFICATION.md) keypoints
to Volume 7 (pose validation) and Volume 8 (ControlNet).

## 3. Formal specification and seed

Spec: **[SPECIFICATION.md](./SPECIFICATION.md)**. Seed:
**[`/database/seed/pose-ontology/`](../../database/seed/pose-ontology/)** (TSV,
JSON-free; keypoints validated against `kura-skeleton-v0`, regions against body
ontology; no production DDL, KTS-HANDOFF-001 §9).
