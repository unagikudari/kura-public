# Kura 1.0 Standard

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Index and normative framing for the eight-volume Kura 1.0 specification.

## 1. Purpose

This directory is the single source of truth for specification-driven development
of Kura 1.0. The source assignment is
[KTS-HANDOFF-001](./KTS-HANDOFF-001-node-handoff-specification.md); the volumes
below elaborate it. These documents describe intended design; they do **not**
claim implementation completion.

## 2. Normative language

Each volume uses **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY**
as defined by RFC 2119 / RFC 8174. A requirement stated at this level is binding
on any Kura 1.0 implementation regardless of node.

## 3. Volumes

| Volume | Title |
|---|---|
| [01](./volume-01-core-architecture.md) | Core Architecture |
| [02](./volume-02-database.md) | Database Specification |
| [03](./volume-03-ontology.md) | Ontology Specification |
| [04](./volume-04-processing.md) | Processing Specification |
| [05](./volume-05-api-ui.md) | API and UI Specification |
| [06](./volume-06-design-bible.md) | Design Bible |
| [07](./volume-07-validation.md) | Validation Specification |
| [08](./volume-08-generator-mapping.md) | Generator Mapping Specification |

## 4. Foundational invariants (binding across all volumes)

- **INV-1** — The canonical semantic object MUST be the **scene**, not the image.
- **INV-2** — Observations MUST be immutable, append-only evidence (ADR-0001).
- **INV-3** — Canonical current truth MUST be derived by a versioned **Resolver**, never stored directly in observations.
- **INV-4** — Canonical persistent state MUST be relational; JSON MUST NOT be used as authoritative persistence (ADR-0002).
- **INV-5** — Geometry, relations, regions, poses, and attributes MUST attach to scene entities (entity-centric model).
- **INV-6** — Body regions, attributes, relations, poses, and applicability rules MUST be data-driven ontology, not hardcoded in application source.
- **INV-7** — Tags, search facets, summaries, and generator prompt terms MUST be treated as **derived projections**, never as canonical state.
- **INV-8** — Generators (ComfyUI first) MUST be adapters behind a generator-agnostic internal representation.

## 5. Collector product profile

[個人コレクター向け製品仕様](./collector-product-profile.md) adds the proposed
collector experience, intuitive import/export, shared UI/MCP operations, and a
low-resource performance contract. It is a draft product profile, not a ninth
ontology volume or evidence of implementation/performance completion. It does
not override the foundational invariants or accepted ADRs.

[取り込み・書き出し契約](./collector-transfer-contract.md) specifies the proposed
original-preservation, job, resume, and round-trip requirements. The
[改善ロードマップ](../notes/collector-improvement-roadmap.md) tracks implementation
order and distinguishes design work from verified runtime behavior.

[目標アーキテクチャ](./collector-target-architecture.md) defines the proposed
responsibility boundaries and performance-oriented data model independently of
legacy table shapes; migration is a separate workstream.

## 6. Related

- [ADRs](../adr/) — accepted architecture decisions.
- [Glossary](../glossary/) — canonical terminology.
- Ontology directories: [`/ontology`](../../ontology/).
- Database placeholder: [`/database`](../../database/).
