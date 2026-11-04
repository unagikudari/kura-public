<script lang="ts" module>
  /** query 文字列から pool 文脈 (pool:N) を導出。両ページ共通の判定。 */
  export function poolIdFromQuery(query: string): number | null {
    const m = query.match(/\bpool:(\d+)\b/);
    return m ? Number(m[1]) : null;
  }
</script>

<script lang="ts">
  // Pool セカンドヘッダー — グリッド (routes/+page.svelte) の pool-title-bar 一式の
  // 抽出 (2026-07-19)。1 本のバー (2026-11-02 から折り畳み無し): pool 名 = 取得元リンク /
  // 取得状況 / アカウント / アーティスト / ⧉ ⿻ 🚫。データ取得 (fetchPoolHeader・pair-search-scope・
  // tagger-exclude) もここが持つ — ページ側は poolId を渡すだけ。
  // post/[id] (マンガ/シングル) でも同じバーを表示する。
  import { poolDisplayName, poolNameParts } from '#lib/site.ts';
  import { fetchPoolHeader, getPairSearchScope, putPairSearchScope, getTaggerExclude, putTaggerExclude } from '#lib/api.ts';
  import { PoolAcquisition } from '#lib/poolIntegrations.ts';
  import type { PoolSummary } from '#lib/types.ts';
  import { tagLocale, tagDisplayName } from '#lib/tagLocale.ts';
  import { t } from '#lib/i18n/index.ts';
  const m = t();

  let { poolId }: { poolId: number } = $props();

  const locale = tagLocale();
  let artists = $state<NonNullable<PoolSummary['artists']>>([]);
  const account = $derived(poolName ? poolNameParts(poolName).account : null);
  const artistNames = $derived(artists.map(tag => tagDisplayName(tag.names[0], tag.localizedNames, $locale)).join('・'));

  let poolName = $state<string | null>(null);
  let poolSource = $state<string | null>(null);
  let verifyStatus = $state<'pending' | 'verified' | 'excluded' | null>(null);
  // 検証プールの focus タグ (server 導出: dashboard-manual:<tag> または
  // tagger_head_candidate 逆引き)。user 2026-08-02:「フォーカスタグが
  // きちんと明示されていない。任務も」— チップ+任務行で常時明示する。
  let focusTag = $state<string | null>(null);
  let loadedPoolId = $state<number | null>(null);

  // Prefer the pool's own source_url recorded AT CREATION (primarySourceUrl from
  // pool_url) over firstPostSource (first post's source, derived at view time and
  // null until the pool is filled). Only surface real http(s) links — the
  // legacy:/kura-merge:/manual:pool sentinels are not external sources.
  function pickPoolSource(p: { primarySourceUrl?: string | null; firstPostSource?: string | null }): string | null {
    const isHttp = (u: unknown): u is string => typeof u === 'string' && /^https?:\/\//.test(u);
    if (isHttp(p.primarySourceUrl)) return p.primarySourceUrl;
    if (isHttp(p.firstPostSource)) return p.firstPostSource;
    return null;
  }

  $effect(() => {
    const id = poolId;
    let active = true;
    artists = []; loadedPoolId = null;
    poolName = null; poolSource = null;
    verifyStatus = null; focusTag = null;
    fetchPoolHeader(id)
      .then(p => {
        if (!active) return;
        loadedPoolId = id;
        artists = p.artists ?? [];
        poolName = p.name;
        poolSource = pickPoolSource(p);
        verifyStatus = ((p as any).verifyStatus ?? null) as typeof verifyStatus;
        focusTag = (p as any).focusTag ?? null;
      })
      .catch(() => { if (!active) return; artists = []; poolName = null; poolSource = null; verifyStatus = null; focusTag = null; });
    return () => { active = false; };
  });

  // pool 単位のペア探索スコープ (censor_pair_search_scope)。null = 未設定。
  let pairScopeEnabled = $state<boolean | null>(null);
  let pairScopeBusy = $state(false);
  $effect(() => {
    const id = poolId;
    pairScopeEnabled = null;
    getPairSearchScope(id)
      .then(r => { pairScopeEnabled = r.enabled; })
      .catch(() => { pairScopeEnabled = null; });
  });
  async function togglePairScope() {
    if (pairScopeBusy) return;
    pairScopeBusy = true;
    try {
      const r = await putPairSearchScope(poolId, !(pairScopeEnabled === true));
      pairScopeEnabled = r.enabled;
    } catch (e) {
      console.error('putPairSearchScope failed', e);
    } finally {
      pairScopeBusy = false;
    }
  }

  // 学習除外pool (2026-07-20 user 要望: 自分の生成物/写真管理用など tagger
  // 自動タグ付けに永続的に触らせたくない pool を一発切替)。ペア探索スコープと
  // 同じ sidecar-table + トグルボタンの流儀。
  let taggerExcluded = $state<boolean | null>(null);
  let taggerExcludeBusy = $state(false);
  $effect(() => {
    const id = poolId;
    taggerExcluded = null;
    getTaggerExclude(id)
      .then(r => { taggerExcluded = r.excluded; })
      .catch(() => { taggerExcluded = null; });
  });
  async function toggleTaggerExclude() {
    if (taggerExcludeBusy) return;
    taggerExcludeBusy = true;
    try {
      const r = await putTaggerExclude(poolId, !(taggerExcluded === true));
      taggerExcluded = r.excluded;
    } catch (e) {
      console.error('putTaggerExclude failed', e);
    } finally {
      taggerExcludeBusy = false;
    }
  }
</script>

{#if poolName}
  <!-- One bar, no fold (2026-11-02 owner: 「展開機能はいらない」). The name is the link to the pool's
       source; the acquisition status sits in the middle; the artists are the links at the right, and
       the pool's three switches follow them. -->
  <div class="pool-title-bar" title={poolDisplayName(poolName)}>
    <span class="pool-title-label">Pool #{poolId}</span>
    {#if poolSource}
      <a class="pool-title-name" href={poolSource} target="_blank" rel="noopener noreferrer"><bdi>{poolDisplayName(poolName)}</bdi></a>
    {:else}
      <bdi class="pool-title-name">{poolDisplayName(poolName)}</bdi>
    {/if}
    <!-- The slot stays as the spacer that keeps the artists at the right without an adapter. -->
    <span class="pool-title-acquisition">{#if PoolAcquisition && loadedPoolId === poolId}<PoolAcquisition {poolId} variant="bar" />{/if}</span>
    <!-- Its own slot, not inside the ellipsized name, so a long title cannot hide it. -->
    {#if account}<a class="pool-account" href={account.url} target="_blank" rel="noopener noreferrer">{account.label} ↗</a>{/if}
    {#if artists.length}<span class="pool-title-artists" title={m.home.poolTitle.artistsTitle(artistNames)} aria-label={m.home.poolTitle.artists}>{#each artists as artist, i (artist.id)}{#if i}・{/if}<a href={`/?q=${encodeURIComponent(artist.names[0])}`}><bdi>{tagDisplayName(artist.names[0], artist.localizedNames, $locale)}</bdi></a>{/each}</span>{/if}
    <a class="pool-switch" href={`/lab/collector/existing?pool=${poolId}`} aria-label={m.home.poolTitle.openInCollector} title={m.home.poolTitle.openInCollector}>⧉</a>
    <button class="pool-switch pool-pair-scope" class:on={pairScopeEnabled === true}
            disabled={pairScopeBusy} onclick={togglePairScope}
            title={m.home.poolTitle.pairScopeTitle} aria-pressed={pairScopeEnabled === true}
            aria-label={`${m.home.poolTitle.pairScope}: ${pairScopeEnabled === true ? 'ON' : pairScopeEnabled === false ? 'OFF' : m.home.poolTitle.unset}`}>
      ⿻{pairScopeEnabled === true ? '●' : pairScopeEnabled === false ? '○' : '–'}
    </button>
    <button class="pool-switch pool-tagger-exclude" class:on={taggerExcluded === true}
            disabled={taggerExcludeBusy} onclick={toggleTaggerExclude}
            title={m.home.poolTitle.taggerExcludeTitle} aria-pressed={taggerExcluded === true}
            aria-label={`${m.home.poolTitle.taggerExclude}: ${taggerExcluded === true ? 'ON' : m.home.poolTitle.unset}`}>
      🚫{taggerExcluded === true ? '●' : '–'}
    </button>
  </div>
  {#if focusTag}
    <div class="pool-focus"><span class="pool-focus-tag" title={m.home.poolTitle.focusTag}>🎯 {focusTag}</span></div>
  {/if}
  {#if focusTag && verifyStatus === 'pending'}
    <div class="pool-mission">
      {m.home.poolTitle.missionBefore}<strong>{focusTag}</strong>{m.home.poolTitle.missionAfter}
    </div>
  {/if}
{/if}

<style>
  .pool-title-bar {
    padding: 0 var(--control-padding-x);
    min-height: var(--control-height);
    box-sizing: border-box;
    min-width: 0;
    background: #1e1a2e;
    /* The bottom line is drawn inside, so touch-size switches keep the bar at one control height. */
    box-shadow: inset 0 -1px #3a2a5a;
    display: flex;
    align-items: center;
    gap: 0.5em;
    font-size: var(--control-font);
    flex-shrink: 0; border-radius: var(--control-radius); }
  .pool-title-label { color: #7a5aaa; font-size: 0.75rem; flex-shrink: 0; }
  .pool-title-name { color: #c9a0ff; font-weight: 500; flex: 0 1 auto; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-decoration: none; }
  a.pool-title-name:hover, a.pool-title-name:focus-visible { text-decoration: underline; }
  .pool-title-acquisition { flex: 0 0 auto; margin: 0 auto; white-space: nowrap; }
  /* The only place the artist links are: a long list scrolls sideways instead of clipping links away. */
  .pool-title-artists { flex: 0 1 auto; max-width: 35%; min-width: 0; white-space: nowrap; overflow-x: auto; overflow-y: hidden; scrollbar-width: none; color: var(--accent); font-size: 11px; }
  .pool-title-artists::-webkit-scrollbar { display: none; }
  .pool-account { flex-shrink: 0; color: var(--accent); font-size: 11px; text-decoration: none; white-space: nowrap; }
  .pool-account:hover, .pool-account:focus-visible { text-decoration: underline; }
  .pool-title-artists a { color: inherit; text-decoration: none; }
  .pool-title-artists a:hover, .pool-title-artists a:focus-visible { text-decoration: underline; }
  /* The pool's switches, small at the end of the bar. */
  .pool-switch {
    flex-shrink: 0; display: inline-flex; align-items: center; justify-content: center;
    font-size: 11px; background: none; border: 1px solid #334; color: #99a; text-decoration: none;
    border-radius: var(--control-radius); padding: 0 4px; cursor: pointer; white-space: nowrap;
    min-height: var(--control-compact-height); box-sizing: border-box; }
  .pool-switch:hover { border-color: #557; color: #c9a0ff; }
  .pool-switch:disabled { opacity: 0.6; cursor: default; }
  .pool-pair-scope.on { color: #8fd19e; border-color: #2a5a3a; background: #14241a; }
  .pool-tagger-exclude.on { color: #e08a8a; border-color: #5a2a2a; background: #241414; }
  @media (max-width: 600px), (pointer: coarse) {
    .pool-title-label { font-size: 11px; }
    .pool-switch { min-height: var(--control-height); min-width: var(--control-height); }
  }
  /* A phone cannot fit the whole bar: rather than squeezing the source link and the artists to
     nothing, the bar is one row that scrolls sideways, and each part keeps a usable width. */
  @media (max-width: 600px) {
    .pool-title-bar { overflow-x: auto; overflow-y: hidden; scrollbar-width: none; }
    .pool-title-bar::-webkit-scrollbar { display: none; }
    .pool-title-name { min-width: 8em; }
    .pool-title-artists { flex: 0 0 auto; max-width: none; overflow: visible; }   /* the bar scrolls, not a list inside it */
  }
  .pool-focus { padding: 2px 10px; min-width: 0; }
  .pool-focus-tag {
    font-size: 0.78rem; color: #e0c97f; border: 1px solid #5a4a2a;
    background: #241f14; border-radius: var(--control-radius); padding: 1px 8px;
    font-family: monospace; overflow-wrap: anywhere;
  }
  .pool-mission {
    padding: 4px 12px; font-size: 0.74rem; color: #b0a890;
    background: #1a1710; border-bottom: 1px solid #3a3222; flex-shrink: 0;
  }
  .pool-mission strong { color: #e0c97f; font-family: monospace; }
</style>
