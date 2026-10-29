# ADR-0003: Body Regions Are Normalized Ontology Entries

- **Status:** Accepted
- **Version:** 0.1.0
- **Date:** 2026-07-26
- **Scope:** How body regions are represented in the ontology and data model.

## Context

Body regions (general and sensitive anatomy) must be referenced consistently by
analysis, validation, and generation. Ad-hoc strings or per-image tags cause drift
and prevent applicability rules from being data-driven.

## Decision

- Body regions MUST be normalized ontology entries with stable identifiers.
- Body regions MUST be modelled neutrally as anatomical terms.
- By default, a body region MUST be a normalized region **belonging to a person
  entity**, not a separate scene entity — unless an operation explicitly requires
  separate relation or geometry semantics.
- Initial region classes MUST include: `surface_skin`, `surface_mucosa`, `hair`,
  `eye`, `mouth`, `external_genital`, `perianal`, `bone_landmark`, `joint`.

## Consequences

- Consistent, data-driven references across analysis/validation/generation.
- Applicability of attributes/relations/poses to regions is expressible as data.
- The full region dataset (identifiers, hierarchy, laterality, keypoints) is
  follow-up work (KTS-HANDOFF-001 §13) and MUST NOT be hardcoded in application
  source.

## Alternatives rejected

- **Free-text region tags** — rejected; unstable, undriveable by rules.
