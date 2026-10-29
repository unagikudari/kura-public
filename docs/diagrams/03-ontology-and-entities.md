# Kura 1.0 — Ontology & Entity Attachment

- **Status:** Draft — **Version:** 0.1.0
- **Scope:** The ontology domains and how they attach to scene entities
  ([Volume 3](../standard/volume-03-ontology.md), the `ontology/*` specs).

## Ontology domains attaching to a scene

```mermaid
flowchart TB
    SCENE([Scene])
    SCENE --> P1([Person entity])
    SCENE --> P2([Person entity])
    SCENE --> OBJ([Object entity])
    SCENE --> SD[/scene descriptors<br/>camera · perspective · rendering · setting/]

    P1 --> BR[body regions<br/><i>presence</i>]
    P1 --> AT[attributes<br/><i>appearance · state · measure</i>]
    P1 --> PO[poses<br/><i>→ kura-skeleton-v0 keypoints</i>]
    P1 --> ID[identity<br/><i>species · character · franchise</i>]
    P1 -. relation .- P2
    P1 -. wearing .- OBJ

    classDef scene fill:#e3f2fd,stroke:#1565c0;
    classDef person fill:#e8f5e9,stroke:#2e7d32;
    classDef object fill:#fff3e0,stroke:#ef6c00;
    class SCENE,SD scene;
    class P1,P2 person;
    class OBJ object;
```

- **Physical** ontologies (body / attribute / pose) describe *what* an entity is;
  **identity** (species / character / franchise) describes *who* it is — kept
  separate (a depiction may deviate from a character's canonical traits).
- **scene descriptors** (camera angle, perspective, rendering, setting) are
  scene-level, not per-entity.

## The ontology layer

```mermaid
flowchart LR
    subgraph physical["Physical (what)"]
        BODY[body<br/>117 regions + kura-skeleton-v0]
        ATTR[attribute<br/>value domains]
        REL[relation<br/>arity · directionality]
        POSE[pose<br/>keypoint bridge]
        OBJ2[object<br/>coverage → body regions]
    end
    subgraph identity["Identity (who)"]
        SPE[species]
        CHA[character]
        FRA[franchise]
    end
    subgraph scene["Scene (context)"]
        SCN[scene descriptors<br/>camera · perspective · rendering]
    end
    BODY --- POSE
    BODY --- OBJ2
    ATTR --- BODY
    CHA --- FRA
    CHA --- SPE
```

Every ontology term is **data** (`database/seed/*`), stable-id'd, and loadable into
the DDL's ontology tables — none hardcoded in application source.
