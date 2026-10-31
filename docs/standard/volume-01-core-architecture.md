# Kura 1.0 Standard — Volume 1: Core Architecture

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Purpose, canonical pipelines, architectural principles, and the
  observation / resolution / projection model that governs every other volume.

## 1. Purpose and scope

Kura is a PostgreSQL-based platform for image understanding, annotation, scene
representation, validation, search, and image generation. This volume defines the
architecture all other volumes refine. It does not describe an implemented system.

## 2. Canonical object

- The system's canonical semantic object MUST be the **scene**.
- An **image** MUST be modelled as an **asset** that represents a scene in one of
  three roles: a source scene, a generation target, or an observed generated output.
- A scene MUST be able to exist and be reasoned about independently of any single
  asset that depicts it.

## 3. Canonical pipelines

### 3.1 Processing

```
Asset → Scene Analysis → Immutable Observation → Resolver → Resolved Scene → Projection
```

- Each stage MUST consume the output of the previous stage without mutating it.
- **Scene Analysis** MUST emit observations only; it MUST NOT write resolved state.
- **Resolved Scene** MUST be produced solely by the Resolver (§5).

### 3.2 Generation and validation loop

```
Resolved Scene → Generation Target → Generation Compiler → Generator Adapter →
Generated Asset → Scene Analysis → Target-versus-Observed Comparison → Constraint Validation
```

- A generated asset MUST re-enter Scene Analysis so that observed output is
  represented with the same observation model as any other asset.
- Constraint Validation MUST compare observed output against the target's
  constraints (see Volume 7).

## 4. Observation model

- Observations MUST be append-only (ADR-0001). A correction MUST be expressed as a
  new observation; prior evidence MUST NOT be overwritten or deleted.
- Every observation MUST record its provenance: producing agent (model or human),
  version, and time.
- Model and human observations MUST share one evidence model; human input MUST NOT
  be stored as a privileged out-of-band override.

## 5. Resolution model

- Canonical current truth MUST be derived by a **versioned Resolver**.
- The Resolver MUST be deterministic given (a fixed observation set, a fixed
  Resolver version).
- Resolver output (the Resolved Scene) MUST be reproducible and MUST record the
  Resolver version that produced it.
- The Resolver MAY be re-run to produce a new resolved version as observations or
  Resolver logic change; prior resolved versions SHOULD remain reconstructable.

## 6. Projection model

- Tags, search facets, summaries, and generator prompt terms MUST be **derived
  projections** of resolved scenes (INV-7).
- Projections MUST be rebuildable from canonical state; they MUST NOT be a source
  of truth.
- A projection MAY be denormalized for performance provided it is reconstructable.

## 7. Component responsibilities (non-binding overview)

| Component | Responsibility |
|---|---|
| Scene Analysis | Produce immutable observations from an asset. |
| Resolver | Derive the resolved scene from observations (versioned). |
| Projection builder | Materialize tags/facets/summaries/prompts from resolved scenes. |
| Generation compiler | Turn a resolved scene + target into generator-agnostic instructions. |
| Generator adapter | Execute instructions on a concrete generator (ComfyUI first). |

## 8. Lifecycle

- Ingest an asset → analyze → observe → resolve → project → (optionally) target →
  generate → observe generated output → compare → validate.
- Each transition MUST preserve the immutability and provenance guarantees above.

## 9. Out of scope for this volume

Concrete DDL, API routes, ontology term lists, and generator wiring are specified
in Volumes 2–8 and MUST NOT be duplicated here.
