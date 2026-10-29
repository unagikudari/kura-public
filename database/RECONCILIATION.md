# Kura 1.0 — Database Reconciliation (legacy → Kura 1.0)

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Reconcile the **existing implementation** (the legacy event-sourced,
  tag-centric schema that migrated production kura) with the **Kura 1.0**
  scene/entity-centric model, so that full production DDL can follow.
- **Source:** [KTS-HANDOFF-001](../docs/standard/KTS-HANDOFF-001-node-handoff-specification.md) §9
  ("Do not add production DDL beyond placeholders until the existing implementation
  has been reconciled"), [Volume 2](../docs/standard/volume-02-database.md).

This document is the §9 gate. It is analysis + normative mapping only; it adds **no
production DDL** ([`schema.sql`](./schema.sql) stays a placeholder). Figures are
measured from the migrated legacy database (nodeb `legacy`), not assumed.

## 1. The existing implementation (legacy), measured

legacy is a tag-centric, event-sourced schema (82 tables) that migrated production
kura: **704,834 assets**, **48,388,453 asset-tag relations**, **~48.36M resolution
events**, **16,626 primary tags (+3,441 aliases)**, collections, provenance, and
model versions. Its event model (`asset_tag_events`, append-only) and versioned
resolution (`resolution_events` → `asset_tag_resolutions`) already embody
event-sourcing.

## 2. The two models

| Aspect | legacy (existing) | Kura 1.0 (target) |
|---|---|---|
| Canonical object | asset (≈ image ≈ kura post) | **scene** |
| Semantic content | flat **tags** on assets | **entities** with geometry/regions/poses/attributes/relations, described by an **ontology** |
| Truth derivation | append-only tag events → Resolver → current tags | append-only **observations** → Resolver → **resolved scene** |
| Tags | the semantic content itself | a **derived projection** of the resolved scene |
| Camera / pose | (flat tags, unstructured) | first-class **scene ontology** (camera) + **pose** domain |

The event-sourcing spine aligns; the semantic model does not. legacy's **flat tags are
the central reconciliation problem**.

## 3. Structural correspondence

| legacy | Kura 1.0 | Disposition |
|---|---|---|
| `blobs`, `blob_backends` | blob store | **carry over** (content-addressed, schema-agnostic) |
| `assets` (uuid5 per post) | `asset` (represents a scene) | **carry over + extend**: each source asset seeds a **scene** it represents |
| `collections`, `collection_assets`, `collection_sources` | collections of scenes/assets | **carry over** |
| `sources`, `crawl_states`, `source_accounts`, `asset_sources` | provenance | **carry over** |
| `asset_tag_events` (append-only) | **observations** | **transform**: each tag event → an observation on a scene/entity (§5) |
| `human_annotation_events`, `annotation_evidence_types` | human observations | **carry over** (already the observation shape) |
| `resolution_events`, `asset_tag_resolutions`, `asset_current_tags` | Resolver / resolved scene / tag projection | **transform**: resolve to a **resolved scene**; `asset_current_tags` becomes a **projection** |
| `tags`, `tag_statuses`, aliases | ontology terms + tag projection | **reinterpret**: tags split across ontology domains (§4); the tag string survives only as a projection label |
| `asset_tag_relations` | (derived) | **drop as source of truth** (it is a projection of events) |
| `model_versions`, `model_version_tags`, `model_tag_thresholds`, `active_models` | model registry | **carry over** |
| `asset_relations` (refined_from), `asset_regions` (bbox), families | asset/derivation + geometry | **carry over**; regions inform entity geometry |
| review pools (`review_*`) | annotation/review workflow | **carry over** |
| — (none) | **scene**, **entity**, **pose**, ontology-typed observation | **new** (§6) |

## 4. The flat-tag conflation (core finding, measured)

legacy's 16,626 tags conflate **at least nine** Kura 1.0 domains. Evidence from the
live top-frequency tags and a random sample:

| Domain (target) | Example legacy tags | Kura 1.0 target |
|---|---|---|
| `body_region` | breasts, nipples, penis, pussy, testicles, navel, ass, pubic_hair | body-ontology region **presence** observation |
| `attribute` | long_hair, black_hair, blue_eyes, large_breasts, blush, sweat, erection, open_mouth, smile | attribute-ontology **value** on entity/region |
| `entity_count` | 1girl, 1boy, solo_focus, multiple_girls, futanari | **entity resolution** (how many/what person entities) |
| `pose` | spread_legs, knees_to_chest, hugging_own_legs, presenting_pussy, sitting_on_lap | **pose** domain (body keypoints) |
| `relation` | hetero, sex, hand_between_legs, holding_legs, finger_in_mouth, hands_on_another's_face | relation-ontology relation between entities/regions |
| `scene_descriptor` | looking_at_viewer, indoors, cityscape, monochrome, greyscale | scene-ontology facet (camera/setting/composition) |
| `object` | black_pantyhose, top_hat, cape, micro_bikini, popsicle, electric_fan | **object entity** / worn/held relation — **ontology gap** (§6) |
| `character` | mash_kyrielight | **entity identity** — **ontology gap** (§6) |
| `meta_nonsemantic` | auto_tagme, tagme, stats, pixiv_username, censored, mosaic_censoring, uncensored | **not** a scene observation — asset-processing metadata / projection state |

**Consequence:** the reconciliation cannot be a table rename. Every tag must be
**classified** into a domain and re-expressed as the right ontology object. This is
exactly the structural upgrade the flat schema could not provide (e.g. camera angle
and pose become queryable — the earlier open question).

### 4.1 合成タグは 1 対 1 で写らない。そして **タグを否定しない**

§4 の表は「タグ 1 つ → domain 1 つ」を前提にしているが、実タグの相当数は
**複数 domain にまたがる連言**である。

```
doggystyle  =  pose all_fours (person_1)
             + relation behind (person_2 → person_1)
             + relation inserting_into (person_2 → person_1)
```

Kura 1.0 側はこの合成語を **ontology に置かない** (`0009` の判断: 1 つの配置を複数の
独立 boolean で言い直すと定義が崩れる)。しかしそれは **タグが誤りだという意味ではない**。

- タグは**粒度の粗い主張**であって、分解と矛盾しない。
- 分解のうち **観測できたものだけ**が観測になる。結合部が遮蔽されていれば
  `inserting_into` は書かれない (非捏造)。
- したがって「タグが言っていること」⊋「観測が言えること」になる場合がある。
  **この差はタガーの誤りとは限らない — 画像がその情報を持っていないだけのことがある。**

両者を突き合わせるには、**タグ側を捨てずに残す**必要がある。タグを「再表現して消費する」
(§5 の re-projection) だけでは、後から「タガーが doggystyle と言ったとき、実際に確認
できたのはどの要素か」を問えない。

### 4.2 そのために要る 3 つの構造 (未実装。§9 の決定事項)

1. **外部ラベルの観測** — 外部の語彙のまま記録する。`doggystyle` は Kura の ontology に
   無いので、ontology 表への FK では縛れない。**別表に置く** — ontology を外部語彙で
   汚さないため。producer は当該タガー (`producer_kind='model'`)。
2. **分解規則** — 外部ラベル → ontology の事実の**連言**。1 対 N であり、かつ
   **仮説**なので、証拠と版を持つ (規則そのものが検証対象になる)。
3. **突き合わせの結果** — ある scene で規則の各要素が
   **成立 / 不成立 / 判定不能** のどれか。
   **判定不能を成立にも不成立にも潰さない**のが要点。潰すとタガーの誤りと画像の限界が
   区別できなくなり、この構造を作った意味が消える。

これがあると測れるもの:

| 問い | 読み方 |
|---|---|
| `doggystyle` の scene で `all_fours` と `behind` が成立した割合 | タガーの精度 |
| `inserting_into` が **判定不能**だった割合 | **画像の限界** (タガーの誤りではない) |
| 分解の要素が揃っているのにタグが無い scene | タガーの取りこぼし |

### 4.3 なぜ今やらないか

plan `kura-1.0-implementation` の S-7 が「**tag は import しない**」と決めている。
kura1 に外部タグを入れると比較が「人手注釈 vs flat tag」でなくなり、utility の測定が
汚れる。この構造は utility 実証後の acquisition / reconciliation フェーズのもので、
債務として [[notes/kura-1.0-deferred-debt]] に登録してある。

## 5. Observation-model alignment (strongest carry-over)

- legacy `asset_tag_events` are append-only with `event_type` ∈ {human_annotation,
  resolution, retraction} and provenance — this **is** the Kura observation shape.
- Reconciliation: an legacy tag event `(asset, tag, event_type, actor, at)` becomes a
  Kura **observation** `(scene, subject, ontology_assertion, producer, at)` where
  `subject` is the scene or a resolved entity/region and `ontology_assertion` is the
  domain-typed target from §4.
- Immutability and provenance carry over unchanged (ADR-0001). No legacy event is
  rewritten; the transform is an **additive re-projection**, not a mutation.

## 6. New concepts legacy lacks (gaps to close)

- **Scene** — legacy has no scene. Reconciliation seeds one scene per source asset
  initially (1 asset ⇒ 1 source scene); multi-asset scenes are later work.
- **Entity** — legacy has no entities. `entity_count` tags (1girl/1boy) drive initial
  entity resolution: create N person entities per scene; region/attribute/pose
  observations attach to an entity (initially the sole/primary person when N=1).
- **Pose** — legacy has no keypoints. Pose tags map to the pose domain; actual keypoint
  geometry is follow-up (body-ontology `kura-skeleton-v0`).
- **Object ontology** — clothing/props are a large tag class with **no Kura 1.0
  ontology yet**. A `/ontology/object` domain MUST be specified before object tags
  can be reconciled (decision needed).
- **Character identity** — named characters need an entity-identity model (decision
  needed).

## 7. Reconciliation path (phased, non-destructive)

1. **Carry-over import** — blobs, assets, collections, provenance, model registry
   move over structurally unchanged; each source asset gets a **scene**.
2. **Tag classification** — build `tag_domain_map` (each of 16,626 tags → domain +
   ontology target). Mechanism + taxonomy + worked sample seeded here
   (`database/seed/reconciliation/`); full classification is LLM-assisted follow-up.
3. **Observation re-projection** — replay legacy tag events as domain-typed
   observations on scenes/entities (additive; legacy events preserved).
4. **Entity resolution** — from `entity_count`, instantiate person entities and
   attach region/attribute/pose observations.
5. **Re-resolution** — the Kura Resolver derives resolved scenes; tags become
   projections (`asset_current_tags`-equivalent rebuilt from resolved scenes).
6. **Validation** — Volume 7 constraints run on resolved scenes.

Each phase is reversible/re-runnable; legacy remains intact as the reconciliation
source throughout.

## 8. DDL implications (unblocks §9)

Full `schema.sql` MUST define, at minimum, these table groups — informed by legacy's
proven schema (carry-over groups) plus the new Kura concepts:

- **Storage**: blobs + backends (from legacy, unchanged).
- **Scenes & assets**: scene, asset (asset↔scene role), asset relations, regions.
- **Entities**: entity (kind: person/object), entity attachments (geometry, region
  instances, poses).
- **Ontology**: term tables per domain (body/attribute/relation/scene/pose/object),
  applicability — loaded from `ontology/*` + `database/seed/*` (relational, no JSON).
- **Observations** (append-only): observation, evidence type, producer/provenance.
- **Resolution/projection**: resolved scene (versioned), tag/facet projections.
- **Provenance, collections, model registry, review**: from legacy, largely unchanged.

Only after this reconciliation is accepted may production DDL be authored (§9).

## 9. Open decisions

- **Object ontology** — specify `/ontology/object` (clothing/props) before object
  tags reconcile.
- **Character identity** — entity-identity model for named characters.
- **Scene granularity** — 1 asset ⇒ 1 scene initially; when do assets share a scene?
- **Meta tag disposition** — confirm `meta_nonsemantic` tags become asset-processing
  metadata/projection state, not scene observations.

## 10. Out of scope

The full 16,626-tag classification dataset, the object/character ontologies, and the
production DDL itself are follow-up work (this document is the gate that unblocks
them).
