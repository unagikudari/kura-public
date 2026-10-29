# Kura 1.0 Standard — Volume 7: Validation Specification

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Human anatomy, pose, support, gravity, contact, occlusion, gaze,
  camera, perspective, and structural constraint evaluation.

## 1. Scope

This volume defines how a resolved scene or a generated asset is validated against
structural and physical constraints. It states evaluation requirements; it does
not implement evaluators.

## 2. Validation model

- Validation MUST operate on resolved scenes and on observations of generated
  assets, not on raw tags.
- Each constraint MUST produce an explicit, inspectable verdict with evidence.
- A validation verdict MUST be recorded as evidence (append-only), not as a
  destructive edit of scene state.

## 3. Constraint domains

Validation MUST be able to evaluate at least the following domains:

- **Anatomy** — anatomical plausibility of person entities and body regions.
- **Pose** — pose configuration consistency (joint/laterality plausibility).
- **Support & gravity** — entities are supported consistently with gravity.
- **Contact** — declared contacts are geometrically plausible.
- **Occlusion** — occlusion relationships are consistent with geometry.
- **Gaze** — gaze direction is consistent with head/eye configuration.
- **Camera & perspective** — camera framing and perspective are internally
  consistent, and consistent with entity geometry.
- **Structural** — general structural/composition constraints.

## 4. Camera and perspective

- Camera and perspective MUST be first-class validation inputs, expressible on the
  scene independently of any single asset.
- Camera/perspective constraints on a **generation target** MUST be comparable
  against the camera/perspective **observed** in the generated asset (§5).

## 5. Target-versus-observed comparison

- For a generated asset, validation MUST compare observed properties against the
  generation target's declared constraints.
- A comparison MUST classify each constraint as satisfied, violated, or
  indeterminate, with evidence.

## 6. Continuous constraints

- Continuous constraints (angles, distances, coverage) MUST use scalar estimates
  with intervals/uncertainty rather than boolean-only checks where practical.

## 7. Out of scope

Concrete thresholds, evaluator code, and calibration datasets are follow-up work.
