# Kura 1.0 — Reconciliation & Resolution

- **Status:** Draft — **Version:** 0.1.0
- **Scope:** How the existing legacy flat-tag data reconciles into Kura 1.0
  ([RECONCILIATION.md](../../database/RECONCILIATION.md),
  [ENTITY-RESOLUTION.md](../../database/ENTITY-RESOLUTION.md)), and how the Resolver
  folds observations into truth ([resolver-specification.md](../standard/resolver-specification.md)).

## legacy flat tags → Kura 1.0 (reconciliation)

```mermaid
flowchart TB
    TAG[legacy flat tag<br/><i>16,626 tags on assets</i>]
    TAG --> CLS{Classify<br/>into domain}
    CLS -->|body| BR[body region presence]
    CLS -->|attribute| AT[attribute value]
    CLS -->|count| EC[entity_count → instantiate]
    CLS -->|pose| PO[pose descriptor]
    CLS -->|relation| RL[relation]
    CLS -->|scene| SD[scene descriptor]
    CLS -->|object| OB[object + state]
    CLS -->|character| ID[identity]
    CLS -->|meta| MX[meta — NOT a scene observation]

    BR & AT & PO & RL & SD & OB & ID --> OBS[(domain-typed<br/>observation)]
    EC --> ENT[entities]
    OBS --> ENT

    classDef drop fill:#ffebee,stroke:#c62828;
    class MX drop;
```

- Reconciliation is **not a rename**: each flat tag is classified and re-expressed as
  the right ontology object. `meta` tags (auto_tagme, censored, stats) are **not**
  scene observations.
- legacy `asset_tag_events` (append-only) map directly to Kura observations — an
  **additive re-projection** that never mutates legacy.

## Attachment & the non-fabrication rule

```mermaid
flowchart TB
    T[person-scoped tag] --> Q{how many<br/>person entities?}
    Q -->|solo / one| E[attach to that entity<br/><b>entity scope</b>]
    Q -->|multiple| U[attach to scene + candidates<br/><b>unresolved scope</b><br/><i>never guessed</i>]
    U -. later disambiguation .-> E

    classDef ok fill:#e8f5e9,stroke:#2e7d32;
    classDef hold fill:#fff8e1,stroke:#f9a825;
    class E ok;
    class U hold;
```

## Observations → Resolver → resolved scene

```mermaid
flowchart LR
    O1[(obs: model)] --> R
    O2[(obs: human)] --> R
    O3[(obs: retraction)] --> R
    R{{Resolver<br/>retraction › human›model › recency}}
    R --> RS[Resolved Scene<br/><b>current</b>]
    R -.->|genuine tie| CT[contested<br/><i>not guessed</i>]
    RS --> TP[tag projection<br/><i>rebuildable</i>]
    RS -.->|re-resolve on new obs| RS2[Resolved Scene<br/>supersedes prior]

    classDef truth fill:#e3f2fd,stroke:#1565c0;
    class RS,RS2 truth;
```

- Conflicts resolve by **retraction › producer precedence (human > model) › recency**,
  over a deterministic order (`observed_at`, then `id`).
- A genuine tie/contradiction is **contested**, not guessed; re-resolution produces a
  **new** resolved scene (old → superseded), never a mutation.
