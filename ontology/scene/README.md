# Scene Ontology

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Scene-level ontology for Kura 1.0. Structure and requirements only;
  the full dataset is follow-up work (KTS-HANDOFF-001 §13).

## 1. Requirements

- Scene-level descriptors MUST be data-driven ontology entries with stable
  identifiers; they MUST NOT be hardcoded in application source.
- The scene ontology MUST support camera and perspective as first-class,
  entity-independent scene properties (Volume 7 §4).
- Scene descriptors MUST be expressible independently of any single asset so the
  same scene can back a source, a target, and a generated output.

## 2. Categories (non-final)

- Composition and framing (including camera and perspective).
- Setting/environment descriptors.
- Scene-level relations among entities.

## 3. Formal specification and seed

Formal spec: **[SPECIFICATION.md](./SPECIFICATION.md)** (facets incl. first-class
camera/perspective, descriptor model, generator mapping). Baseline seed:
**[`/database/seed/scene-ontology/`](../../database/seed/scene-ontology/)** (TSV,
JSON-free; no production DDL, KTS-HANDOFF-001 §9).
