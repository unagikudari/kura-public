# Resolver Policy Seed

- **Status:** Draft — **Version:** 0.1.0
- **Scope:** The data-driven resolution policy for
  [`docs/standard/resolver-specification.md`](../../../docs/standard/resolver-specification.md).
  TSV, JSON-free; no production DDL yet (the policy tables are a follow-up migration —
  the Resolver reads policy from data, not hardcode).

| File | Rows | Describes |
|---|---|---|
| `producer_precedence.tsv` | 2 | producer rank — human (2) outranks model (1) |
| `per_domain_policy.tsv` | 9 | per-domain resolution strategy |
| `conflict_outcomes.tsv` | 4 | resolved / retracted / **contested** / **unresolved** |

## Key policies

- **Global order:** retraction → producer precedence (human > model) → recency
  (deterministic total order: `observed_at`, then `id`).
- **Continuous attributes:** human point estimate wins; else median of model
  estimates with a `[min,max]` interval (uncertainty-preserving, Volume 1 §6).
- **Truthfulness:** a genuine tie/contradiction is **contested** (not guessed); a
  person-scoped tag in a multi-entity scene is **unresolved** (not attached). These
  mirror the entity-resolution non-fabrication rule — the Resolver never invents a
  value the observations don't support.
