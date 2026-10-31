# Kura 1.0 Standard — Volume 4: Processing Specification

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Scene analysis, observation ingestion, Resolver behaviour,
  comparison-based annotation, calibration, generation compilation, comparison,
  and projection building.

## 1. Scope

This volume defines the runtime behaviour of the processing pipeline from Volume 1.
It states contracts, not an implementation.

## 2. Scene analysis

- Scene analysis MUST take an asset and emit observations only.
- It MUST NOT write resolved state or projections.
- Each emitted observation MUST carry producer, version, and time provenance.

## 3. Observation ingestion

- Ingestion MUST be append-only; it MUST reject any attempt to update or delete an
  existing observation.
- A correction MUST be ingested as a new observation referencing what it corrects.
- Ingestion MUST validate that referenced entities/terms exist in the ontology.

## 4. Resolver behaviour

- The Resolver MUST derive a resolved scene from the current observation set for a
  scene, using a declared Resolver version.
- Resolution MUST be deterministic for a fixed (observation set, Resolver version).
- The Resolver MUST record which observations and which version produced a resolved
  scene, so the result is reconstructable.
- Re-resolution on new evidence MUST produce a new resolved version; it MUST NOT
  mutate a prior resolved version in place.

## 5. Comparison-based annotation and calibration

- Human annotation SHOULD be captured as comparison against model evidence and
  reference points rather than as free-form overrides.
- Calibration data (how model scores relate to reference judgements) MUST be
  versioned and treated as observations/evidence, not as canonical truth.

## 6. Generation compilation and comparison

- The generation compiler MUST turn (resolved scene + generation target) into a
  generator-agnostic instruction set (Volume 8).
- After generation, the generated asset MUST re-enter scene analysis.
- Target-versus-observed comparison MUST evaluate the observed output against the
  target's declared constraints (Volume 7).

## 7. Projection building

- Projection builders MUST read resolved scenes and MUST NOT read raw observations
  as if they were truth.
- A projection MUST be fully rebuildable from canonical state.
- Projection staleness MUST be detectable (e.g. via resolved-version watermarks).

## 8. Out of scope

Concrete worker code, queue mechanics, and model wiring are Volume 5 / follow-up.
