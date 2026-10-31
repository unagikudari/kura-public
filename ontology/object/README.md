# Object Ontology

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Objects (clothing, accessories, props, furniture, environment) as scene
  entities. Formal spec: **[SPECIFICATION.md](./SPECIFICATION.md)**.

## 1. Requirements

- Objects MUST be data-driven ontology entries with stable identifiers
  `kura:object/<slug>`; they MUST NOT be hardcoded in application source.
- An object is a scene **entity of kind `object`** (distinct from `person`); geometry,
  attributes, and relations attach to it (INV-5).
- Each object MUST declare a **category**; clothing MUST declare a **slot** and the
  body regions it **covers** (referencing the body ontology).

## 2. Why this exists

The [database reconciliation](../../database/RECONCILIATION.md) surfaced a large class
of legacy tags — clothing and props (pantyhose, top_hat, popsicle…) — with no Kura 1.0
target. This ontology is that target: it lets clothing coverage feed occlusion
validation (Volume 7) and inpaint/generation targeting (Volume 8), and lets state
tags (`no_panties`, `open_shirt`, `torn_pantyhose`) reconcile to `(object, state)`.

## 3. Formal specification and seed

Spec: **[SPECIFICATION.md](./SPECIFICATION.md)**. Seed:
**[`/database/seed/object-ontology/`](../../database/seed/object-ontology/)** (TSV,
JSON-free; coverage validated against body-ontology regions; no production DDL,
KTS-HANDOFF-001 §9).
