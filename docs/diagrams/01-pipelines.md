# Kura 1.0 — Pipelines

- **Status:** Draft — **Version:** 0.1.0
- **Scope:** The canonical processing pipeline and the generation/validation loop
  ([Volume 1](../standard/volume-01-core-architecture.md) §3). Mermaid, git-tracked.

## Processing pipeline

```mermaid
flowchart LR
    A[Asset<br/><i>image</i>] --> SA[Scene Analysis]
    SA -->|emits| O[(Immutable<br/>Observation)]
    O --> R{{Resolver<br/><i>versioned</i>}}
    R --> RS[Resolved Scene<br/><i>canonical truth</i>]
    RS --> P[Projection<br/><i>tags · facets · prompts</i>]

    classDef truth fill:#e3f2fd,stroke:#1565c0;
    classDef derived fill:#f3e5f5,stroke:#6a1b9a;
    classDef evidence fill:#fff8e1,stroke:#f9a825;
    class RS truth;
    class P derived;
    class O evidence;
```

- Each stage consumes the previous **without mutating** it.
- Scene Analysis emits **observations only**; the **Resolver** alone produces the
  resolved scene; **projections** are derived and rebuildable (never truth).

## Generation & validation loop

```mermaid
flowchart LR
    RS[Resolved Scene] --> GT[Generation Target]
    GT --> GC[Generation Compiler]
    GC --> GA[Generator Adapter<br/><i>ComfyUI = first adapter</i>]
    GA --> GO[Generated Asset]
    GO --> SA[Scene Analysis]
    SA --> CMP[Target vs Observed<br/>Comparison]
    GT -. constraints .-> CMP
    CMP --> CV[Constraint Validation<br/><i>Volume 7</i>]

    classDef truth fill:#e3f2fd,stroke:#1565c0;
    class RS truth;
```

- A generated asset **re-enters Scene Analysis**, so observed output is represented
  with the same observation model as any asset.
- The generator is an **adapter behind a generator-agnostic representation** (INV-8).
