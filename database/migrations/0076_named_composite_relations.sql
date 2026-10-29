-- Migration 0076 — 名前が中身を決めている 2 人の形も 1 語にする
--
-- plan kura-1.0-implementation、2026-07-31 の user 指摘:
-- 「**フルネルソンもそうだろ**。あれは全身ポーズ関係 p1 フルネルソン p2 で表現できる。
--   背後に立って右手がわきの下でとかに**分解する必要はない**。
--   **そういう関係を内包したセットなんだから**」
--
-- ── 0075 の規則は関係にも当たる ──
-- 0075 で「**確定的な名前のある形は 1 語**。条件は説明に書き、注釈者に分解させない」と
-- 決めた (正座を両足に分けて書かないのと同じ)。**2 人の形でも同じ**で、
-- 0071 で羽交い締めを behind + restraining(arm→axilla)×2 に分解したのは行き過ぎだった。
--
-- ── 0054 と矛盾しない (境界はここ) ──
-- 0054 では punch / kick を作らず `striking` + 主体の部位にした。違いは:
--   * punch と kick は**どこで打つかが画像ごとに変わる**。語を作ると部位の数だけ増える
--   * 羽交い締めは**名前が中身を決めている**。後ろから、両腕を腋の下へ、が固定
-- **判定は「名前が部品を決めているか」** — 決めているなら 1 語、変わるなら分解。
-- (0019 / 0049 / 0060 / 0075 と同じ「語がすでに決めていることを別の軸で書かせない」)
--
-- ── 内包しているものは別に書かない ──
-- full_nelson は「後ろにいる」を内包するので、`behind` を別に書かない (二重の真実)。
-- 説明に「何を内包しているか」を書いておけば、後から導ける。
--
-- ── 一般形は残す ──
-- `restraining` は消さない。手首を掴む / 体重で押さえる / 縄で縛る のように
-- **形が決まっていない拘束**はそちらで書く。
--
-- ロールバック: 追加した relation を DELETE、型の展開を 0071 の 3 本へ戻す。

BEGIN;
SET LOCAL lock_timeout = '5s';
SET search_path = kura, public;

INSERT INTO relation(slug, display_name, category, directionality) VALUES
  ('full_nelson', '羽交い締め', 'interaction', 'directed')
ON CONFLICT (slug) DO NOTHING;

UPDATE relation SET description =
  '羽交い締め。$s が $o の**後ろから両腕を腋の下に通して動きを封じている**。'
  '**名前が中身を決めているので 1 語** (0076、user 指摘) — 後ろから / 両腕 / 腋の下 が'
  '固定なので、`behind` や `restraining (arm → axilla)` を**別に書かない** (二重の真実)。'
  '**形が決まっていない拘束は restraining** — 手首を掴む / 体重で押さえる / 縄で縛る。'
  '**punch / kick を作らなかった 0054 と矛盾しない** — あちらは打つ部位が画像ごとに'
  '変わるので語を分けると部位の数だけ増える。判定は**名前が部品を決めているか**。'
  '実測 full_nelson 13,066'
WHERE slug = 'full_nelson';

INSERT INTO relation_applicability(relation_slug, role, participant_kind, participant_key) VALUES
  ('full_nelson', 'subject', 'entity', 'person'),
  ('full_nelson', 'object',  'entity', 'person')
ON CONFLICT DO NOTHING;

UPDATE relation SET description = coalesce(description,'') ||
  '。**羽交い締めのように名前が中身を決めている形は 1 語** (full_nelson、0076) — '
  'こちらは形が決まっていない拘束に使う'
WHERE slug = 'restraining' AND coalesce(description,'') NOT LIKE '%full_nelson%';

-- 型は 1 語を書くほうへ移す (同じ事実を 2 通りで書かない)
DELETE FROM phrase_macro_relation WHERE macro_slug = 'full_nelson';
INSERT INTO phrase_macro_relation
  (macro_slug, ord, relation_slug, subject_ref, object_ref) VALUES
  ('full_nelson', 1, 'full_nelson', '$s', '$o');

UPDATE phrase_macro SET description =
  '$s が $o を**羽交い締めにしている**。**関係 1 語で書く** (0076) — '
  '後ろから / 両腕 / 腋の下 は名前が決めているので分解しない。'
  '形が決まっていない拘束は `restraining` を直接使う'
WHERE slug = 'full_nelson';

COMMIT;
