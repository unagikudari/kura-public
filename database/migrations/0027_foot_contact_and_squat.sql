-- Migration 0027 — 接地の仕方を姿勢から分離する (蹲踞が書けなかった)
--
-- plan kura-1.0-implementation の S-8 中の指摘、2026-07-29:
-- 「そんきょは pose 座り方の表現のどれに当たる？」→ **当たるものが無い**。
--
-- ── なぜ書けなかったか ──
-- `squatting` の定義が「**足裏を付けたまま**膝を深く曲げ、臀部は床に付かない」で、
-- **「深くしゃがむ」と「足のどこで接地しているか」を 1 語に固めていた**。蹲踞は踵を
-- 上げて爪先で支えるので、この定義では当てはまらない。`crouching` は上体を前に落とす
-- 浅い姿勢なので別物。
--
-- これは `doggystyle` を足さなかったのと同じ構図 — **合成されたものを 1 語にすると、
-- 構成要素の一部だけが違う姿勢が書けなくなる**。0009 では語を足さずに分解の仕方を
-- description に書いたが、ここは**分解に必要な軸そのものが無かった**ので軸を作る。
--
-- ── 足す軸: 接地の仕方 ──
-- `foot_contact` (legs scope の pose ではなく **attribute**)。理由:
--   * 姿勢ではなく「足のどの面が地に付いているか」という状態で、立位・しゃがみ・
--     膝立ちのどれとも独立に成り立つ (つま先立ちで立つ / つま先立ちでしゃがむ)。
--   * pose にすると `same scope conflict → latest wins` で squatting と競合し、
--     片方が捨てられる (0023 で trembling を pose にしなかったのと同じ理由)。
--   * region=foot に付けば **左右も書ける** (0010 の laterality) — 片足だけ踵を
--     上げている、が書ける。
-- 値: plantar (足裏全面) / ball (拇指球・つま先立ち) / heel (踵のみ) / airborne (接地なし)。
-- 裏付け: tiptoes 14,591 / soles 31,351 / barefoot 74,454 (barefoot は着衣の話なので別)。
--
-- ── `squatting` の定義から接地を外す ──
-- 「足裏を付けたまま」を削る。接地は `foot_contact` で言う。**既存の観測は壊れない** —
-- squatting はこれまでも「深くしゃがむ」を意味しており、外すのは付随していた条件だけ。
--
-- ── 蹲踞をどう書くか (語は足さない) ──
--   squatting + foot_contact(foot)=ball + spread_legs
-- 相撲・剣道の蹲踞はこの 3 つで表せる。**`sonkyo` という語は置かない** — 文化固有の
-- 名前であって体の構えの原子ではなく、置くと「蹲踞に見えるが踵が付いている」等が
-- 書けなくなる (B1 の指摘の再来)。分解の仕方は description に書く。
--
-- ── 運用手順 ──
--   * 単一 transaction。attribute → 値 → 説明。冪等。
--   * ロールバック: 追加した attribute / attribute_value を DELETE、squatting の
--     description を戻す。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO attribute(slug, display_name, category, value_type, applies_kind, applies_key) VALUES
  ('foot_contact', 'foot contact', 'state', 'categorical', 'region', 'foot')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('foot_contact','plantar'), ('foot_contact','ball'),
  ('foot_contact','heel'),    ('foot_contact','airborne')
ON CONFLICT DO NOTHING;

UPDATE attribute SET description =
  '足のどの面が地に付いているか。plantar / ball / heel / airborne。'
  '**姿勢とは独立** — つま先立ちで立つことも、つま先立ちでしゃがむ (蹲踞) こともある。'
  'region=foot に付くので **左右を書き分けられる** (片足だけ踵を上げている 等)'
WHERE slug = 'foot_contact';

UPDATE attribute_value SET description = v.d FROM (VALUES
  ('foot_contact','plantar',  '足裏全面が接地している'),
  ('foot_contact','ball',     '拇指球・爪先で支えている (つま先立ち)。踵は浮いている'),
  ('foot_contact','heel',     '踵だけが接地している'),
  ('foot_contact','airborne', '接地していない')
) AS v(a,s,d) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

UPDATE attribute_value SET display_order = v.o FROM (VALUES
  ('foot_contact','plantar',1),('foot_contact','ball',2),
  ('foot_contact','heel',3),('foot_contact','airborne',4)
) AS v(a,s,o) WHERE attribute_value.attribute_slug = v.a AND attribute_value.value_slug = v.s;

-- =====================================================================
-- squatting から接地の条件を外し、蹲踞の書き方を description に書く
-- =====================================================================
UPDATE pose SET description =
  'しゃがんでいる。膝を深く曲げ、臀部は床に付かない。**接地の仕方は含まない** — '
  '足裏で支えるか爪先で支えるかは属性 foot_contact で書く。'
  '**蹲踞 (相撲・剣道) はこれ + foot_contact=ball + spread_legs** で表す '
  '(sonkyo という語は置かない — 文化固有の名前であって体の構えの原子ではなく、'
  '置くと「蹲踞に見えるが踵が付いている」が書けなくなる)'
WHERE slug = 'squatting';

UPDATE pose SET description =
  'かがんでいる。squatting より浅く、上体を前に落とした姿勢。'
  '**深くしゃがんでいるなら squatting**'
WHERE slug = 'crouching';

COMMIT;
