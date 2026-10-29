# Scene Ontology Seed

- **Status:** Draft — **Version:** 0.1.0
- **Scope:** Baseline scene-descriptor vocabulary per
  [`ontology/scene/SPECIFICATION.md`](../../../ontology/scene/SPECIFICATION.md).
  TSV, JSON-free authoring data; no production DDL (KTS-HANDOFF-001 §9).

| File | Describes |
|---|---|
| `scene_facets.tsv` | the 7 scene facets (incl. first-class `camera_angle`, `perspective`, and `rendering`) |
| `scene_descriptors.tsv` | descriptors per facet (slug, display, facet) |
| `scene_descriptor_prompt_map.tsv` | derived-projection generator prompt hints |

**Stable id:** `kura:scene/<slug>`. Every descriptor belongs to exactly one facet
(verified at authoring time). Camera angle and shot type are first-class so a
generation target's camera can be compared against an observed camera (Volume 7 §5).

**Gap-fill (reconciliation):** the `rendering` facet (colored / monochrome /
greyscale / sepia / limited_palette / spot_color / lineart / sketch) resolves the
image-rendering legacy tags (monochrome, greyscale) that had no scene target.
