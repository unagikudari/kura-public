# Kura 1.0 — Node Handoff Specification

Implementation-ready assignment for a separate development node.

| Field | Value |
|---|---|
| Document ID | KTS-HANDOFF-001 |
| Status | Approved for execution |
| Version | 1.0 |
| Repository | `unagikudari/kura` |
| Target branch | `agent/kura-1.0-foundation` |
| Date | 2026-07-26 |

> This markdown is the faithful, version-controlled transcription of the source
> handoff (`Kura_1.0_Node_Handoff_Specification.docx`). It is the source
> specification from which the eight standard volumes, ADRs, and ontology
> directories in this branch derive. It does not itself claim implementation
> completion.

## 1. Purpose

This document instructs a separate implementation node to establish the
specification-driven foundation for Kura 1.0 in the existing private GitHub
repository. The node shall create a dedicated branch, add the initial standards
and ontology documentation structure, commit the work, push it, and open a draft
pull request without modifying `main` directly.

## 2. Project context

Kura is a PostgreSQL-based image understanding, annotation, scene representation,
validation, search, and image-generation platform. The canonical semantic object
is the **scene**, not the image. Images are assets that represent source scenes,
generation targets, or observed generated outputs.

**Canonical processing pipeline:**

```
Asset → Scene Analysis → Immutable Observation → Resolver → Resolved Scene → Projection
```

**Generation and validation loop:**

```
Resolved Scene → Generation Target → Generation Compiler → Generator Adapter →
Generated Asset → Scene Analysis → Target-versus-Observed Comparison → Constraint Validation
```

## 3. Non-negotiable design decisions

| Decision | Required interpretation |
|---|---|
| Immutable observations | Model and human observations are append-only evidence. Corrections are new observations; previous evidence is not overwritten. |
| Resolver-owned truth | Canonical current truth is derived by a versioned Resolver, not stored directly in observations. |
| Scene-first representation | Images are assets; resolved scenes are canonical semantic state. |
| Relational persistence | PostgreSQL is authoritative. JSON is prohibited as canonical persistent state and may only be used at transport or ephemeral boundaries. |
| Entity-centric data model | Geometry, relations, regions, poses, and attributes attach to scene entities. |
| Continuous representation | Continuous properties use scalar estimates, intervals, model evidence, and human reference comparisons where practical. |
| Ontology-driven behavior | Body regions, attributes, relations, poses, and applicability rules are data-driven and not hardcoded. |
| Generator agnosticism | ComfyUI is the first adapter, not the canonical internal representation. |
| Derived projections | Tags, search facets, summaries, and generator-specific prompt terms are derived projections. |

## 4. Required Git workflow

The node shall perform the following workflow exactly:

1. Clone or open the repository `unagikudari/kura`.
2. Confirm the default branch is `main` and inspect the working tree.
3. Do not modify or push directly to `main`.
4. Create branch `agent/kura-1.0-foundation` from the latest `main`.
5. Add only the files and directories defined in this document.
6. Run Markdown and repository sanity checks available in the repository.
7. Commit with message: `Establish Kura 1.0 specification foundation`.
8. Push the branch to origin with upstream tracking.
9. Open a draft pull request targeting `main`.
10. Do not merge the pull request.

## 5. Required repository structure

```
docs/
├── standard/
│   ├── README.md
│   ├── volume-01-core-architecture.md
│   ├── volume-02-database.md
│   ├── volume-03-ontology.md
│   ├── volume-04-processing.md
│   ├── volume-05-api-ui.md
│   ├── volume-06-design-bible.md
│   ├── volume-07-validation.md
│   └── volume-08-generator-mapping.md
├── adr/
│   ├── ADR-0001-immutable-observations.md
│   ├── ADR-0002-relational-persistence.md
│   └── ADR-0003-normalized-body-regions.md
├── glossary/
│   └── README.md
├── diagrams/
└── examples/
database/
├── README.md
├── schema.sql
├── migrations/
├── seed/
└── erd/
ontology/
├── body/
│   └── README.md
├── attributes/
│   └── README.md
├── relations/
│   └── README.md
└── scene/
    └── README.md
```

## 6. Required volume content

| Volume | Content |
|---|---|
| 1 — Core Architecture | Purpose, scope, canonical pipelines, architectural principles, observation model, resolution model, projection model, component responsibilities, and lifecycle. |
| 2 — Database Specification | PostgreSQL domains, table responsibilities, constraints, indexes, migrations, provenance, data ownership, and the prohibition on authoritative JSON persistence. |
| 3 — Ontology Specification | Body, object, scene, pose, attribute, and relation ontologies; hierarchy, laterality, applicability, stable identifiers, and generator mappings. |
| 4 — Processing Specification | Scene analysis, observation ingestion, Resolver behavior, comparison-based annotation, calibration, generation compilation, comparison, and projection building. |
| 5 — API and UI Specification | REST interfaces, worker contracts, queue operations, viewer, annotation UI, generation UI, comparison UI, and search. |
| 6 — Design Bible | Rationale, rejected alternatives, trade-offs, and long-term design constraints. |
| 7 — Validation Specification | Human anatomy, pose, support, gravity, contact, occlusion, gaze, camera, perspective, and structural constraint evaluation. |
| 8 — Generator Mapping Specification | Transformation from resolved scene and target constraints to prompts, LoRA, ControlNet, regional conditions, sampler configuration, and generator adapters. |

## 7. Body ontology baseline

The initial body ontology must explicitly support normalized, stable entries for
general anatomy and sensitive anatomical regions. These are anatomical terms and
must be modeled neutrally.

- `body, head, face, forehead, cheek, nose, eye, iris, pupil, mouth, neck`
- `torso, left breast, right breast, left areola, right areola, left nipple, right nipple`
- `pelvis, pubic region, external genitalia, penis, scrotum, vulva, perineum, anus`
- `left arm, right arm, left hand, right hand, left leg, right leg, left foot, right foot`

Body regions are not separate scene entities by default. They are normalized
regions belonging to a person entity unless an operation explicitly requires
separate relation or geometry semantics.

**Initial region classes:** `surface_skin`, `surface_mucosa`, `hair`, `eye`,
`mouth`, `external_genital`, `perianal`, `bone_landmark`, `joint`.

## 8. Initial ADRs

| ID | Title | Status |
|---|---|---|
| ADR-0001 | Observations Are Immutable | Accepted |
| ADR-0002 | Canonical Persistence Is Relational | Accepted |
| ADR-0003 | Body Regions Are Normalized Ontology Entries | Accepted |

## 9. File-content requirements

- All standard files shall contain a title, status, version, scope, and initial normative statements.
- Use MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY consistently for normative requirements.
- Do not claim implementation completion.
- Do not add production DDL beyond placeholders until the existing implementation has been reconciled.
- Do not delete, relocate, or rewrite existing project files unless required to resolve a direct path conflict.
- Do not introduce JSON columns as canonical storage.
- Do not hardcode ontology lists in application source as part of this foundation task.
- Use UTF-8 Markdown.

## 10. Draft pull request specification

**PR title:** `Establish Kura 1.0 specification foundation`

**PR body shall contain:**

- **What changed:** initial eight-volume specification structure, ADRs, glossary, database placeholder, and ontology directories.
- **Why:** establish a single source of truth for specification-driven development.
- **Impact:** documentation-only foundation; no production behavior change.
- **Validation:** list checks performed and confirm `main` was not modified directly.
- **Follow-up:** Body Ontology formalization, database reconciliation, full DDL, Resolver specification, and diagrams.

The PR must remain in Draft state.

## 11. Acceptance criteria

- [ ] Branch `agent/kura-1.0-foundation` exists on origin.
- [ ] The branch is based on the latest `main` at the time of execution.
- [ ] All required paths exist.
- [ ] Eight standard volume files exist.
- [ ] Three ADR files exist with Accepted status.
- [ ] Glossary and ontology README files exist.
- [ ] No direct commit was made to `main`.
- [ ] A single intentional commit contains the foundation changes, unless repository hooks require an additional formatting commit.
- [ ] A draft pull request targets `main`.
- [ ] The PR reports validation results and outstanding follow-up work.
- [ ] No existing application behavior is modified.

## 12. Out of scope

- Implementing the complete database schema.
- Migrating existing production data.
- Implementing the Resolver.
- Implementing ComfyUI integration.
- Creating the full body-region ontology dataset.
- Changing existing frontend or backend behavior.
- Merging the pull request.

## 13. Next node assignment after foundation

After the foundation PR is reviewed, the next recommended assignment is the formal
Body Ontology specification and seed dataset. That task should define stable
identifiers, hierarchy, laterality, region classes, applicability rules, keypoint
mappings, annotation units, and generator-edit units.

## 14. Execution report format

```
Repository:
Branch:
Base commit:
Commit SHA:
Draft PR:
Files created:
Checks run:
Check results:
Conflicts or deviations:
Recommended next task:
```
