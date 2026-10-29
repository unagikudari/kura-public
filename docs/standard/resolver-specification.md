# Kura 1.0 — Resolver Specification

- **Status:** Draft
- **Version:** 0.2.0 — aligned to migrations 0002/0003 (2026-07-28). §2/§4/§6/§7 now name
  the tables that actually exist; before this the document described a design whose
  landing tables (`resolved_entity_binding`, `unresolved_subject_candidate`,
  `resolved_facet`) had no counterpart in the schema.
- **Scope:** The detailed behaviour of the **Resolver** — how the append-only
  observation set for a scene is folded into one **resolved scene** (current truth).
  Expands [Volume 1](./volume-01-core-architecture.md) §5 and
  [Volume 4](./volume-04-processing.md) §4; populates the DDL tables `resolver_run`,
  `resolved_scene`, `resolved_observation`, `tag_projection`
  ([database/schema.sql](../../database/schema.sql)) plus the tables added by
  [migration 0002](../../database/migrations/0002_resolver_owned_entity_binding.sql)
  (`resolved_entity_binding`, run-scoped `entity`) and
  [migration 0003](../../database/migrations/0003_nonfabrication_landing.sql)
  (`unresolved_subject_candidate`, `resolved_facet`, `resolved_facet_evidence`, `unit`).
- **Governing:** ADR-0001 (immutable observations), [ENTITY-RESOLUTION.md](../../database/ENTITY-RESOLUTION.md) (non-fabrication).

The resolution **policy** is data-driven (seed
[`/database/seed/resolver/`](../../database/seed/resolver/)); this document is the
normative behaviour it encodes.

## 1. Contract

- The Resolver takes **(a scene's observation set, a Resolver version)** and produces
  **one resolved scene**.
- It MUST be **deterministic**: identical inputs → identical output.
- It MUST be **reproducible**: the resolved scene records its `resolver_run`
  (version) and the observations it accepted (`resolved_observation`).
- It MUST NOT mutate observations or a prior resolved scene (ADR-0001); re-resolution
  produces a **new** `resolved_scene` (§7).

## 2. Determinism — total order on observations

- Observations MUST be processed in a fixed total order: `observed_at` ascending,
  then `observation.id` ascending as a deterministic tiebreak. Both columns are on
  `observation`; `id` is `generated always as identity`, so the tiebreak is total even
  when two observations share a timestamp (the common case for one annotation session,
  where every row of a scene lands within the same second).
- All aggregation (§5) MUST be order-independent or applied over this total order, so
  no wall-clock, randomness, or set-iteration order affects the result.

## 3. Global conflict rules

Applied per **resolution key** — the (subject, predicate) a value attaches to
(e.g. `(entity 7, attribute eye_color)`, `(entity 7, region breast present)`):

1. **Retraction** — an observation with `event_type='retraction'` (or a later
   `explicit_remove`) removes the assertion it targets (`retracts_id`, or the same
   resolution key). A retracted assertion MUST NOT appear in resolved truth.
2. **Producer precedence** — human observations outrank model observations
   (`producer_precedence.tsv`: human > model). A human assertion wins over a
   conflicting model assertion regardless of time.
3. **Recency** — within the same precedence tier, the latest observation (by §2
   order) wins.
4. **Contested** — if two observations of the **same** precedence tier assert
   **conflicting** values and neither is later-and-same-key-overriding (a genuine
   tie, or contradictory human corrections), the key is marked **contested** and left
   **unresolved for that value** — the Resolver MUST NOT pick arbitrarily (§6).

## 4. Entity resolution phase (runs first)

Per [ENTITY-RESOLUTION.md](../../database/ENTITY-RESOLUTION.md):

0. **`entity` is the Resolver's output, not an input** (migration 0002). Every
   `entity` row carries `resolver_run_id` and the table has `UNIQUE (id, resolver_run_id)`;
   `resolved_entity_binding`, `entity_region` and `obs_relation` reference entities by
   the **composite** key `(entity_id, resolver_run_id)`, so a binding produced by one run
   cannot point at an entity created by another. Cross-run mixing is rejected by the FK,
   not by convention. A re-resolution therefore creates a fresh entity set; earlier runs
   stay intact and readable.
1. Resolve `entity_count` observations to a person count per presentation
   (exact counts override lower-bound `multiple_*`; §3 precedence/recency applies).
   Instantiate that many `person` entities; instantiate `object` entities from
   object observations. `solo` ⇒ exactly one; `no_humans` ⇒ zero persons.
2. **Attachment**: a person-scoped observation attaches to a specific `entity`
   **only when unambiguous** (single person entity), and the attachment is written to
   `resolved_entity_binding (resolver_run_id, observation_id, entity_id)`.
   In a multi-person scene it MUST remain `unresolved`, **never guessed onto an
   entity** — the non-fabrication rule, enforced by the DDL scope CHECK.
3. **The candidate set is stored, not implied.** An unresolved observation records the
   entities it *could* belong to in
   `unresolved_subject_candidate (resolver_run_id, observation_id, entity_id, rank, exclusivity)`.
   `rank` orders the candidates (1 = most likely) and `exclusivity` says whether the
   candidates are mutually exclusive (`exclusive` — exactly one of them holds it) or not
   (`inclusive` — several may). Without these two columns "unresolved" degenerates to
   "we lost the information"; with them a later disambiguation is a *refinement* of a
   recorded claim rather than a fresh guess.
4. **Ingest-time references.** Human annotation names subjects by `subject_ref`
   (`person_1`), not by entity id — entities do not exist until this phase runs.
   `obs_relation` therefore carries `subject_ref` / `subject_region_slug` /
   `object_ref` / `object_region_slug` alongside the resolved entity columns
   (migration 0004), with a CHECK allowing **at most one participant form per role** and
   **zero** participants for a genuinely one-sided relation.

## 5. Per-domain resolution policies

| Domain | Policy (`per_domain_policy.tsv`) |
|---|---|
| `body_region` | presence = latest non-retracted; a `present=false` removal wins by §3. |
| `attribute` (categorical / boolean) | one value per (entity, attribute) by precedence-then-recency; conflicting same-tier ⇒ **contested**. |
| `attribute` (continuous) | a human point estimate wins; otherwise aggregate model estimates as the **median** with an interval `[min,max]` of the estimates — deterministic, uncertainty-preserving (Volume 1 §6). |
| `pose` | resolved pose is the **set union** of non-retracted poses per entity; two poses of the **same scope** conflict ⇒ latest wins (§2 order). |
| `relation` | set of non-retracted relations; participants resolved only as far as evidence allows (unresolved otherwise). |
| `scene_descriptor` | one primary descriptor **per facet** (latest); scene-scoped. |
| `identity` | one species / character per entity by precedence-then-recency. |

Each resolved value is one `resolved_facet` row. The table has **no `facet_key text`
column**: the subject of the fact is given by typed FKs (`region_slug`,
`attribute_slug`, `pose_slug`, `relation_slug`, `descriptor_slug`, `object_slug`,
`species_slug`, `character_slug`, `presentation`), so an unknown or misspelled facet
cannot be represented at all. Continuous values use `value_numeric` with
`interval_low` / `interval_high`; the unit and permitted range are **not** repeated
here — they are derived from `attribute` (whose `unit` is an FK into the `unit`
vocabulary, migration 0003), because two places to state a scale is two places to
disagree.

## 6. Contested and uncertain state (truthfulness)

- A **contested** resolution key MUST be represented as contested in the resolved
  scene, not collapsed to a guess. A truthful "unknown/contested" is a correct
  outcome; an arbitrary pick is a defect (mirrors the entity-resolution
  non-fabrication rule).
- Contested state is a value of `resolved_facet.status` (vocabulary
  `resolved_facet_status`), not a nullable side-channel — a facet is `resolved` or
  `contested`, and both are first-class.
- **The conflicting observations are kept.** `resolved_facet_evidence
  (resolved_facet_id, observation_id, role)` records each contributing observation and
  its `role`, so a contested facet can be re-examined without re-deriving it: one can
  see *which* observations disagreed. This is the same reason §8 exists for accepted
  observations — a conclusion whose evidence was discarded cannot be audited.
- `unresolved`-scope observations remain unattached; they still inform scene-level
  facts (e.g. "two hairstyles exist") without asserting which entity holds which. Their
  candidate sets live in `unresolved_subject_candidate` (§4.3).

## 7. Versioning and re-resolution

- Each resolution runs under a `resolver_run(resolver_version)`. The resolved scene
  it produces is `status='current'`; any prior current resolved scene for that scene
  becomes `status='superseded'` (the DDL's partial unique index
  `ux_resolved_scene_current` enforces exactly one current).
- Re-resolution occurs on **new observations** (including disambiguations) or a **new
  Resolver version**; it never edits a prior resolved scene. Because `entity` is
  run-scoped (§4.0), a re-resolution also mints a new entity set — an entity id is only
  meaningful **together with** its `resolver_run_id`. Consumers that cache entity ids
  across runs are reading a different claim than the one they stored.
- Because inputs and version are recorded, any historical resolved scene is
  reconstructable.

## 8. Provenance of truth

- The Resolver MUST record, in `resolved_observation`, exactly which observations it
  accepted into the resolved scene — so every resolved fact is traceable to its
  evidence.

## 9. Projection building

- After resolution, tag/facet **projections** are built from the resolved scene into
  `tag_projection` (and other projection tables). Tags are **derived** (INV-7):
  rebuildable from the resolved scene, never canonical truth. A projection MUST be
  reproducible from the resolved scene alone.

## 10. Data-driven policy

Policy vocabularies are seeded ([`/database/seed/resolver/`](../../database/seed/resolver/)):
`producer_precedence.tsv`, `per_domain_policy.tsv`, `conflict_outcomes.tsv`. A future
migration adds the policy tables; the Resolver reads policy from data, not hardcode.

## 11. Conformance / out of scope

Conforms if resolution is deterministic (§2), honours the global conflict rules (§3),
runs entity resolution with non-fabrication (§4/§6), applies the per-domain policies
(§5), versions correctly (§7), and records accepted-observation provenance (§8).
Concrete continuous-aggregation tuning, calibration weighting of model producers, and
the policy DDL are follow-up.
