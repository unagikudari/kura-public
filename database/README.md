# Kura 1.0 Database

- **Status:** Draft
- **Version:** 0.1.0
- **Scope:** Placeholder for the Kura 1.0 relational schema, migrations, seed data,
  and ERD exports. Normative requirements live in
  [Volume 2](../docs/standard/volume-02-database.md) and
  [ADR-0002](../docs/adr/ADR-0002-relational-persistence.md).

## 1. Status

**Production DDL is now defined** — the §9 reconciliation gate is cleared
(RECONCILIATION.md + ENTITY-RESOLUTION.md), so `schema.sql` holds the Kura 1.0
v0.1.0 canonical schema (65 tables), verified on PostgreSQL 18. Forward changes go
through `migrations/` (0002+); `schema.sql` is the flattened baseline snapshot,
recorded as `migrations/0001_kura_1_0_initial.sql`.

The reconciliation that gates DDL is **[RECONCILIATION.md](./RECONCILIATION.md)**
(legacy → Kura 1.0): the structural correspondence, the flat-tag → ontology mapping,
the phased path, and the DDL implications. Its supporting seed is
[`seed/reconciliation/`](./seed/reconciliation/).

**[ENTITY-RESOLUTION.md](./ENTITY-RESOLUTION.md)** extends it with the entity-
resolution semantics — how per-asset flat tags become per-entity observations,
gender presentation vs anatomy, and the conservative non-fabrication rule for
ambiguous multi-entity attachment (seed: [`seed/entity-resolution/`](./seed/entity-resolution/)).

## 2. Layout

| Path | Purpose |
|---|---|
| `schema.sql` | Canonical Kura 1.0 v0.1.0 schema (65 tables), verified on PG 18. |
| `migrations/` | Ordered, forward-only migrations (empty in the foundation). |
| `seed/` | Seed datasets, e.g. ontology seeds (empty in the foundation). |
| `erd/` | Generated ERD exports (empty in the foundation). |

## 3. Rules (binding)

- PostgreSQL MUST be the authoritative store (ADR-0002).
- JSON MUST NOT be used as canonical persistent state.
- Observation tables MUST be append-only (ADR-0001).
- Schema changes MUST arrive as reviewable migrations under `migrations/`.

## 4. Follow-up

Database reconciliation with the existing implementation and full DDL are
follow-up work (KTS-HANDOFF-001 §13).

## 用語 — 所見 / 確定所見 (2026-07-30 確定)

タグに代わる分析の単位を **所見 (しょけん)** と呼ぶ (user 判断)。読影所見と同じ含みで、
「**誰かが見て述べた**」が語に入っている — タグに無かった性質 (主体束縛と発言者) がそれ。

| 日本語 | 英語 | schema | 実数 (2026-07-30) |
|---|---|---|---|
| **所見** | observation | `observation` | 975 件 (1 scene あたり 16.5) |
| **確定所見** | resolved facet | `resolved_facet` | 854 件 |

**新しい英語名は作らない。** `finding` を当てると 3 つ目の名前が生まれ、
「1 つの事実が 2 か所に住む」形になる (この設計が繰り返し排除してきたもの)。
**所見 = observation の日本語**であって別概念ではない。`観測` は「測る」寄りで
人手の記述に硬かったので**日本語側だけを差し替えた**。

**`resolved observation` は使えない** — `resolved_observation` 表が既にあり
(確定 scene と採用された所見の対応)、別の意味を持っている。だから確定形は
`resolved_facet` = 確定所見。

**表名は変えない。** 改名は資産 (resolver / verify / s11_run / annotate_scene) に
広く波及して得るものが無い。名前は 1 層 1 つ、日英の対で持つ。

### 所見 1 件の形
「**誰の・どこの・何が・どうである**」+ 誰がいつ言ったか (`producer_ref` / `evidence`)。
domain 別の実数: body_region 356 / identity 250 / attribute 240 / relation 83 /
pose 34 / scene_descriptor 20。

タグとの違いは 4 つ: **主体に束縛される** / **発言者が残る** / **撤回できる**
(append-only なので消さず撤回所見を積む) / **軸が分かれている** (1 件 1 軸)。
