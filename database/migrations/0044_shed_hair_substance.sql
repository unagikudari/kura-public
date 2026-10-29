-- Migration 0044 — 体表に付着した毛 (抜け毛・移った毛) を書けるようにする
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘:
-- 「transfar hair がない。汚れの一種でいいと思う」
--
-- ── 訂正 (2026-07-31、user 指摘) ──
-- 当初このコメントは「flat tag 側に存在しない概念」と書いたが**誤り**だった。
-- 私が探した語 (transfer_hair / hair_transfer / loose_hair …) が違っただけで、
-- tag 表には **`transferred_pubic_hair_*` が部位ごとに 5 語**ある:
--   transferred_pubic_hair_mouth 14 / _breast 4 / _stomach 0 / _ass 0 / _face 0
--
-- **むしろこちらの方が要点をよく示している。** タグ空間は同じ事実を**部位ごとの
-- 合成語 5 つ**で持ち、しかも **3 語は 0 件**という空セルを抱えている
-- (組合せ爆発の典型)。所見側は「どの部位に何が付いているか」の軸を既に持っているので、
-- **値を 1 つ足すだけ**で 5 語ぶんが書ける。0008 で体毛を部位ごとに作らず
-- hair_density にしたのと同じ構図。
-- 関連する既存タグ: stray_pubic_hair 64,800 / pubic_hair_peek 7,189 は
-- 「はみ出している」で別の事実 (付着ではない)。
--
-- ── 生えている毛と付いている毛を混同しない (ここが要点) ──
-- `hair` という語は既に 2 箇所にある:
--   * **region** — `pubic_hair` / `armpit_hair` / `beard` … = **生えている**部位。
--     量は `hair_density`、色は `hair_color`
--   * **今回足す value** — `surface_substance='hair'` = **抜けて体表に付いている**もの。
--     生えている場所とは無関係で、腹にも顔にも付く
-- 同じ語で別の事実なので、description に境界を書く。混ぜると
-- 「陰毛が生えていない人の腹に陰毛が付いている」が書けなくなる。
--
-- ── 誰の毛かは書けない (既知の限界。捏造しない) ──
-- `surface_substance` は「その部位に何が付いているか」だけを言う軸で、
-- **由来の人物は持たない**。semen / saliva も同じで、誰のものかは書けていない。
-- ここで毛にだけ由来を持たせると軸が非対称になるので持たせない。
-- 由来まで要るなら relation 側に「誰の何が誰の何に付着している」を作る話になり、
-- それは別の設計 (所見の穴として記録する)。
--
-- ── 運用手順 ──
--   * 単一 transaction。値の追加 → 説明 → 隣の語に境界。冪等。
--   * ロールバック: 追加した attribute_value を DELETE (所見が付いていれば消せない)。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO attribute_value(attribute_slug, value_slug) VALUES
  ('surface_substance','hair')
ON CONFLICT DO NOTHING;

UPDATE attribute_value SET
  description =
    '**抜けて体表に付いている毛**。自分のでも相手のでもよい (由来は書けない — '
    'semen / saliva と同じで、この軸は「何が付いているか」だけを言う)。'
    '**生えている毛は region** (pubic_hair / armpit_hair / beard …) で、量は hair_density、'
    '色は hair_color。同じ「毛」でも別の事実 — 混ぜると「陰毛が生えていない人の腹に'
    '陰毛が付いている」が書けなくなる',
  display_order = 15
WHERE attribute_slug = 'surface_substance' AND value_slug = 'hair';

UPDATE attribute SET description = coalesce(description,'') ||
  '。**付着した毛も値のひとつ** (0044) — 生えている毛は region 側 (pubic_hair 等) で、'
  'ここは体表に付いているものを言う'
WHERE slug = 'surface_substance' AND coalesce(description,'') NOT LIKE '%付着した毛%';

COMMIT;
