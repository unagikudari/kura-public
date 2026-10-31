# Kura 1.0 Glossary

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Canonical terminology for Kura 1.0. Terms here are normative; other
  volumes MUST use them consistently.

## Terms

- **Scene** — the canonical semantic object. The thing depicted, independent of any
  single image.
- **Asset** — a binary or derived artifact (e.g. an image) that represents a scene
  as a source, a generation target, or an observed generated output.
- **Observation** — append-only evidence about a scene produced by a model or a
  human, with provenance (producer, version, time). See ADR-0001.
- **Resolver** — the versioned, deterministic component that derives canonical
  current truth (a resolved scene) from a scene's observations.
- **Resolved Scene** — the canonical current semantic state of a scene, produced by
  the Resolver and reconstructable from observations + Resolver version.
- **Projection** — a derived, rebuildable materialization of resolved state (tags,
  search facets, summaries, generator prompt terms). Never canonical truth.
- **Entity** — a scene-level object (e.g. a person) to which geometry, relations,
  regions, poses, and attributes attach.
- **Body region** — a normalized ontology entry belonging by default to a person
  entity. See ADR-0003.
- **Pose** — a first-class ontology domain describing a person entity's
  configuration, expressible independently of any single image.
- **Generation Target** — a desired scene/constraint set to be generated.
- **Generation Compiler** — turns (resolved scene + target) into a generator-
  agnostic instruction set.
- **Generator Adapter** — translates the instruction set to a concrete generator
  (ComfyUI first).
- **Constraint Validation** — evaluation of a scene/generated asset against
  structural and physical constraints (Volume 7).

## Convention

Normative keywords (MUST, MUST NOT, SHOULD, SHOULD NOT, MAY) follow RFC 2119 /
RFC 8174.
