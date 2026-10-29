-- post.tagging_* と post_tagging_work が一致しているか (docs/notes/tagging-work-table-plan.md 段 1)。
-- 1 文 = 1 スナップショットで比べる。鏡写しは post の更新と同じコミットに入るので、
-- 処理中の lease も一致するはずで、差は本当の不整合。欠けた行・余った行も数える。
-- 段 3 のあとは post の列が止まるので、この照合は段 1〜2 の間だけ意味を持つ。
SELECT
  count(*) FILTER (WHERE w.post_id IS NULL)                           AS missing_in_work,
  count(*) FILTER (WHERE p.id IS NULL)                                AS orphan_in_work,
  count(*) FILTER (WHERE p.id IS NOT NULL AND w.post_id IS NOT NULL
    AND (p.tagging_status, p.tagging_locked_until, p.tagging_attempts, p.tagging_last_error)
        IS DISTINCT FROM (w.status, w.lease_until, w.attempts, w.last_error)) AS differing
FROM public.post p
FULL JOIN public.post_tagging_work w ON w.post_id = p.id;
