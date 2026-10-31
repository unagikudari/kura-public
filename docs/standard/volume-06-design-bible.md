# Kura 1.0 Standard — Volume 6: Design Bible

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Rationale, rejected alternatives, trade-offs, and long-term design
  constraints behind the Kura 1.0 architecture.

## 1. Scope

This volume records *why* Kura 1.0 is shaped as it is. It is normative about the
constraints that must not be casually reversed; it is explanatory about rationale.

## 2. Why scene-first

- Modelling the **scene** as canonical (not the image) lets one semantic state back
  many assets (source, target, generated output) and supports the generation loop.
- **Constraint:** implementations MUST NOT collapse scene state into per-image tags
  as the source of truth.

## 3. Why immutable observations + Resolver

- Append-only observations plus a versioned Resolver give reproducibility, audit,
  and safe correction (a correction is new evidence, not a destructive edit).
- **Rejected alternative:** mutable "current tags" as truth — rejected because it
  loses provenance and makes model/human disagreement unrecoverable.

## 4. Why relational, not JSON

- Relational persistence gives constraints, referential integrity, and queryable
  structure that authoritative JSON blobs cannot guarantee.
- **Rejected alternative:** JSON documents as canonical state — rejected (ADR-0002);
  JSON is a transport/ephemeral convenience only.

## 5. Why entity-centric ontology

- Attaching geometry, relations, regions, poses, and attributes to entities lets
  the same ontology drive analysis, validation, and generation.
- **Rejected alternative:** hardcoded term lists in application code — rejected;
  ontology MUST be data-driven so it can evolve without code changes.

## 6. Why generator-agnostic

- Keeping generators as adapters behind an internal representation prevents any one
  tool (ComfyUI first) from becoming the canonical model.
- **Trade-off:** an extra compilation step (Volume 8) in exchange for portability.

## 7. Long-term constraints (MUST NOT reverse without a superseding ADR)

- Scene-first canonical object (INV-1).
- Immutable observations (INV-2 / ADR-0001).
- Resolver-owned truth (INV-3).
- Relational canonical persistence (INV-4 / ADR-0002).
- Data-driven ontology (INV-6).
- Derived-only projections (INV-7).

## 8. Out of scope

Detailed trade-off analyses per subsystem are added as those subsystems are
specified.
