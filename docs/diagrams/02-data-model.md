# Kura 1.0 — Data Model

- **Status:** Draft — **Version:** 0.1.0
- **Scope:** The core of the canonical schema
  ([database/schema.sql](../../database/schema.sql)) — scenes, assets, entities, the
  append-only observation model, and Resolver-owned resolution. Ontology tables are
  summarised in [03-ontology-and-entities.md](./03-ontology-and-entities.md).

## Core entity-relationship

```mermaid
erDiagram
    SCENE  ||--o{ ASSET       : "depicted by"
    SCENE  ||--o{ ENTITY      : "contains"
    BLOB   ||--o{ ASSET       : "stores"
    ENTITY ||--o{ ENTITY_REGION : "has region instance"
    OBJECT_TYPE ||--o{ ENTITY : "is a (kind=object)"

    SCENE  ||--o{ OBSERVATION : "about"
    ENTITY ||--o{ OBSERVATION : "subject (entity scope)"
    OBSERVATION ||--o| OBS_BODY_REGION     : "detail"
    OBSERVATION ||--o| OBS_ATTRIBUTE       : "detail"
    OBSERVATION ||--o| OBS_POSE            : "detail"
    OBSERVATION ||--o| OBS_RELATION        : "detail"
    OBSERVATION ||--o| OBS_SCENE_DESCRIPTOR: "detail"
    OBSERVATION ||--o| OBS_IDENTITY        : "detail"
    OBSERVATION ||--o| OBS_OBJECT          : "detail"
    OBSERVATION ||--o| OBS_ENTITY_COUNT    : "detail"
    OBSERVATION ||--o{ OBSERVATION : "retracts"

    SCENE       ||--o{ RESOLVED_SCENE : "resolved into"
    RESOLVER_RUN ||--o{ RESOLVED_SCENE : "by version"
    RESOLVED_SCENE ||--o{ RESOLVED_OBSERVATION : "accepted"
    OBSERVATION    ||--o{ RESOLVED_OBSERVATION : "into truth"
    RESOLVED_SCENE ||--o{ TAG_PROJECTION : "derives"

    OBSERVATION {
        bigint id PK
        uuid   uuid
        text   subject_scope "entity|scene|unresolved"
        bigint subject_entity_id FK "NULL unless entity"
        text   domain
        text   event_type
        text   producer_kind
        bigint retracts_id FK
        timestamptz observed_at
    }
    ENTITY {
        bigint id PK
        text   kind "person|object"
        text   gender_presentation "persons only"
        text   object_slug "objects only"
    }
    RESOLVED_SCENE {
        bigint id PK
        bigint resolver_run_id FK
        text   status "current|superseded"
    }
```

## What the schema enforces (verified on PG18)

```mermaid
flowchart TB
    subgraph INV["Invariants enforced in DDL"]
        A["append-only observations<br/>UPDATE/DELETE → trigger raises<br/><i>ADR-0001</i>"]
        B["no JSON canonical<br/>0 json/jsonb columns<br/><i>ADR-0002</i>"]
        C["entity CHECK<br/>person ⊄ object_slug"]
        D["scope CHECK<br/>entity ⇒ entity_id NOT NULL<br/>unresolved ⇒ NULL ok<br/><i>non-fabrication</i>"]
        E["one current resolution<br/>partial unique index"]
    end
```

- `subject_scope='unresolved'` with a NULL entity is the **non-fabrication** outcome:
  a person-scoped fact in a multi-entity scene is recorded without guessing which
  entity holds it.
- Each `observation` carries one per-domain detail row (relational, JSON-free);
  corrections are **new** observations, retractions set `retracts_id`.
