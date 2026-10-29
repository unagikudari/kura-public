# Kura 1.0 Standard — Volume 8: Generator Mapping Specification

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Transformation from resolved scene and target constraints to prompts,
  LoRA, ControlNet, regional conditions, sampler configuration, and generator
  adapters.

## 1. Scope

This volume defines how a resolved scene and a generation target are compiled into
concrete generator instructions. It states requirements; it does not wire any
specific generator.

## 2. Generator agnosticism

- The compiler MUST produce a **generator-agnostic** instruction set from
  (resolved scene + target constraints).
- A **generator adapter** MUST translate that instruction set to a concrete
  generator. ComfyUI MUST be treated as the first adapter, not the canonical
  representation (INV-8).

## 3. Derived-projection status

- Prompts, LoRA selections, ControlNet hints, regional conditions, and sampler
  configuration MUST be **derived projections** of canonical state (INV-7).
- These artifacts MUST be rebuildable from the resolved scene, target, and ontology
  mappings; they MUST NOT be stored as canonical truth.

## 4. Mapping inputs

- Compilation MUST consume: the resolved scene, the generation target's
  constraints (Volume 7), and ontology generator-mappings (Volume 3 §6).
- Continuous target constraints MUST be carried into generator terms with their
  intervals/uncertainty preserved where the generator supports it.

## 5. Instruction set structure

The generator-agnostic instruction set MUST be able to express, at minimum:

- Positive/negative semantic terms (prompt fragments).
- Model/LoRA selection and weights.
- Spatial conditioning (ControlNet / pose skeleton / regional masks).
- Sampler and scheduler configuration.

## 6. Round-trip

- After generation, the generated asset MUST re-enter scene analysis so that its
  observed properties can be compared against the target (Volume 4 §6, Volume 7 §5).

## 7. Out of scope

Concrete adapter code, node graphs, and model inventories are follow-up work.
