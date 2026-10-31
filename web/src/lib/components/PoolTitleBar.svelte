<script lang="ts" module>
  /** query 文字列から pool 文脈 (pool:N) を導出。両ページ共通の判定。 */
  export function poolIdFromQuery(query: string): number | null {
    const m = query.match(/\bpool:(\d+)\b/);
    return m ? Number(m[1]) : null;
  }
</script>

<script lang="ts">
  // Pool セカンドヘッダー — グリッド (routes/+page.svelte) の pool-title-bar 一式の
  // 抽出 (2026-07-19)。pool 名 / 検証チップ / tagger_source / source リンク /
  // +N sources 展開 / ⿻ ペア探索トグル。データ取得 (fetchPoolHeader・pair-search-scope・
  // verify-status) もここが持つ — ページ側は poolId を渡すだけ。
  // post/[id] (マンガ/シングル) でも同じバーを表示する。
  import { fetchPoolHeader, getPairSearchScope, putPairSearchScope, getTaggerExclude, putTaggerExclude, setPrimarySource, setPoolVerifyStatus } from '#lib/api.ts';
  import { PoolAcquisition } from '#lib/poolIntegrations.ts';
  import { poolSourceNote } from '#lib/extensions.ts';
  import PoolVerifyBadge from '#lib/components/PoolVerifyBadge.svelte';
  import type { PoolSource, PoolSummary } from '#lib/types.ts';
  import { tagLocale, tagDisplayName } from '#lib/tagLocale.ts';
  import { t } from '#lib/i18n/index.ts';
  const m = t();

  let { poolId }: { poolId: number } = $props();

  const locale = tagLocale();
  let artists = $state<NonNullable<PoolSummary['artists']>>([]);
  const artistNames = $derived(artists.map(tag => tagDisplayName(tag.names[0], tag.localizedNames, $locale)).join('・'));

  let poolName = $state<string | null>(null);
  let poolSource = $state<string | null>(null);
  let verifyStatus = $state<'pending' | 'verified' | 'excluded' | null>(null);
  let taggerSource = $state<string | null>(null);
  // 検証プールの focus タグ (server 導出: dashboard-manual:<tag> または
  // tagger_head_candidate 逆引き)。user 2026-08-02:「フォーカスタグが
  // きちんと明示されていない。任務も」— チップ+任務行で常時明示する。
  let focusTag = $state<string | null>(null);
  let loadedPoolId = $state<number | null>(null);
  let sources = $state<PoolSource[]>([]);
  let poolExtensions = $state<PoolSummary['extensions']>({});
  let sourcesExpanded = $state(false);
  let poolDetailsOpen = $state(false);

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
    poolName = null; poolSource = null; sources = []; poolExtensions = {}; sourcesExpanded = false; poolDetailsOpen = false;
    verifyStatus = null; taggerSource = null; focusTag = null;
    fetchPoolHeader(id)
      .then(p => {
        if (!active) return;
        loadedPoolId = id;
        artists = p.artists ?? [];
        poolName = p.name;
        poolSource = pickPoolSource(p);
        sources = p.sources ?? [];
        poolExtensions = p.extensions ?? {};
        verifyStatus = ((p as any).verifyStatus ?? null) as typeof verifyStatus;
        taggerSource = (p as any).taggerSource ?? null;
        focusTag = (p as any).focusTag ?? null;
      })
      .catch(() => { if (!active) return; artists = []; poolName = null; poolSource = null; sources = []; verifyStatus = null; taggerSource = null; focusTag = null; });
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

  // Promote an alias source to primary, then refresh the pool's source view.
  let promotingUrl = $state<string | null>(null);
  async function handlePromoteSource(url: string) {
    promotingUrl = url;
    try {
      const p = await setPrimarySource(poolId, url);
      poolSource = pickPoolSource(p);
      sources = p.sources ?? [];
    } catch (e) {
      console.error('setPrimarySource failed', e);
    } finally {
      promotingUrl = null;
    }
  }

  async function toggleVerifyStatus() {
    // 未設定(null) → pending (検証対象にする) → verified → pending の循環。
    // 未設定チップも常時表示する (2026-07-19 user 指示: セカンドヘッダーの
    // 未設定項目も pool ビューでは常に出す — ペア探索と同じ流儀)。
    // head candidate (0075) はここではダイアログで聞かない (user指示「変な
    // ダイアログを出すな」) — verified 切替をサーバ側がトリガに、pool内で
    // 人間が実際に補正した回数(post_tag_audit, actor_type='human')から
    // 自動選出する。人間の入力は一切不要。
    const next = verifyStatus == null ? 'pending'
      : verifyStatus === 'verified' ? 'pending' : 'verified';
    try {
      await setPoolVerifyStatus(poolId, next);
      verifyStatus = next;
    } catch (e) { console.error('verify-status toggle failed', e); }
  }

  // 学習/検証プール指定の解除 (2026-07-20 user 要望) — cycle には含めず
  // 別ボタン (誤クリックでの解除を防ぐ)。null に戻すのみ、post 本体には触れない。
  async function releaseVerifyStatus() {
    try {
      await setPoolVerifyStatus(poolId, null);
      verifyStatus = null;
    } catch (e) { console.error('verify-status release failed', e); }
  }
</script>

{#if poolName}
  <details class="pool-info" bind:open={poolDetailsOpen}>
    <summary class="pool-title-bar" title={poolName}>
      <span class="pool-title-label">Pool #{poolId}</span>
      <bdi class="pool-title-name">{poolName}</bdi>
      {#if artists.length}<bdi class="pool-title-artists" title={m.home.poolTitle.artistsTitle(artistNames)}>{artistNames}</bdi>{/if}
      <span class="pool-title-chevron" aria-hidden="true">⌄</span>
    </summary>
    <div class="pool-details">
      <bdi class="pool-full-name">{poolName}</bdi>
      {#if artists.length}
        <div class="pool-artists" aria-label={m.home.poolTitle.artists}>
          <span class="pool-artists-label">{m.home.poolTitle.artists}</span>
          {#each artists as artist (artist.id)}
            <a href={`/?q=${encodeURIComponent(artist.names[0])}`}><bdi>{tagDisplayName(artist.names[0], artist.localizedNames, $locale)}</bdi></a>
          {/each}
        </div>
      {/if}
      <div class="pool-actions">
    <a href={`/lab/collector/existing?pool=${poolId}`} aria-label={m.home.poolTitle.openInCollector} title={m.home.poolTitle.openInCollector}>⧉</a>
    <PoolVerifyBadge variant="chip" status={verifyStatus} onToggle={toggleVerifyStatus} onRelease={releaseVerifyStatus} />
    {#if taggerSource}
      <span class="pool-tagger-source" title={m.home.poolTitle.taggerSource}>{taggerSource}</span>
    {/if}
    {#if poolSource}
      <a class="pool-title-source" href={poolSource} target="_blank" rel="noopener noreferrer">{poolSource}</a>
    {/if}
    {#if sources.length > 1}
      <button class="pool-sources-toggle" onclick={() => sourcesExpanded = !sourcesExpanded}
              title={m.home.poolTitle.allSources} aria-expanded={sourcesExpanded}
              aria-label={m.home.poolTitle.moreSources(sources.length - 1)}>
        +{sources.length - 1} {sourcesExpanded ? '▴' : '▾'}
      </button>
    {/if}
    <button class="pool-pair-scope" class:on={pairScopeEnabled === true}
            disabled={pairScopeBusy} onclick={togglePairScope}
            title={m.home.poolTitle.pairScopeTitle} aria-pressed={pairScopeEnabled === true}
            aria-label={`${m.home.poolTitle.pairScope}: ${pairScopeEnabled === true ? 'ON' : pairScopeEnabled === false ? 'OFF' : m.home.poolTitle.unset}`}>
      ⿻ {pairScopeEnabled === true ? '●' : pairScopeEnabled === false ? '○' : '–'}
    </button>
    <button class="pool-tagger-exclude" class:on={taggerExcluded === true}
            disabled={taggerExcludeBusy} onclick={toggleTaggerExclude}
            title={m.home.poolTitle.taggerExcludeTitle} aria-pressed={taggerExcluded === true}
            aria-label={`${m.home.poolTitle.taggerExclude}: ${taggerExcluded === true ? 'ON' : m.home.poolTitle.unset}`}>
      🚫{taggerExcluded === true ? '●' : '–'}
    </button>
      </div>
  {#if sourcesExpanded && sources.length > 1}
    <ul class="pool-sources-list">
      {#each sources as s (s.url)}
        <li class="pool-source-row" class:primary={s.isPrimary}>
          <span class="pool-source-pri" title={s.isPrimary ? 'primary source' : 'alias'}>{s.isPrimary ? '★' : '·'}</span>
          {#if s.isExternal}
            <a class="pool-source-url" href={s.url} target="_blank" rel="noopener noreferrer">{s.url}</a>
          {:else}
            <span class="pool-source-url muted">{s.url}</span>
          {/if}
          {#if poolSourceNote(poolExtensions, s.url)}
            <span class="pool-source-note">{poolSourceNote(poolExtensions, s.url)}</span>
          {/if}
          {#if !s.isPrimary && s.isExternal}
            <button class="pool-source-promote" disabled={promotingUrl === s.url} aria-label={promotingUrl === s.url ? m.home.poolTitle.makingPrimary : m.home.poolTitle.makePrimary} title={promotingUrl === s.url ? m.home.poolTitle.makingPrimary : m.home.poolTitle.makePrimary}
                    onclick={() => handlePromoteSource(s.url)}>
              {promotingUrl === s.url ? '…' : '★'}
            </button>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
    {#if poolDetailsOpen && loadedPoolId === poolId && PoolAcquisition}<PoolAcquisition {poolId} />{/if}
    </div>
  </details>
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
    cursor: pointer;
    list-style: none;
    background: #1e1a2e;
    border-bottom: 1px solid #3a2a5a;
    display: flex;
    align-items: center;
    gap: 0.5em;
    font-size: var(--control-font);
    flex-shrink: 0; border-radius: var(--control-radius); }
  .pool-title-label { color: #7a5aaa; font-size: 0.75rem; flex-shrink: 0; }
  .pool-info { min-width: 0; flex-shrink: 0; }
  .pool-title-bar::-webkit-details-marker { display: none; }
  .pool-title-name { color: #c9a0ff; font-weight: 500; flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .pool-title-artists { flex: 0 1 auto; max-width: 35%; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--accent); font-size: 11px; }
  .pool-artists { display: flex; flex-wrap: wrap; align-items: center; gap: var(--control-gap); margin-bottom: 3px; font-size: 12px; }
  .pool-artists-label { font-size: 10px; color: var(--muted); }
  .pool-artists a { display: inline-flex; align-items: center; min-height: var(--control-compact-height); max-width: 100%; color: var(--accent); overflow-wrap: anywhere; }
  .pool-title-chevron { flex: 0 0 auto; color: var(--muted); }
  .pool-info[open] .pool-title-chevron { transform: rotate(180deg); }
  .pool-details { padding: 4px 8px; background: var(--surface); border-bottom: 1px solid var(--line); max-height: min(50dvh, 24rem); overflow-y: auto; box-sizing: border-box; }
  .pool-full-name { display: block; overflow-wrap: anywhere; font-size: 13px; line-height: 1.5; margin-bottom: 4px; }
  .pool-actions { display: flex; align-items: center; flex-wrap: wrap; gap: var(--control-gap); min-width: 0; }
  .pool-actions > :global(*) { min-width: 0; max-width: 100%; box-sizing: border-box; }
  .pool-actions > button { margin-left: 0; }
  .pool-actions .pool-title-source { margin-left: 0; flex: 1 1 100%; max-width: 100%; }
  @media (max-width: 600px), (pointer: coarse) {
    .pool-title-bar { min-height: var(--control-height); font-size: var(--control-font); box-sizing: border-box; padding: 0 var(--control-padding-x); border-radius: var(--control-radius); }
    .pool-title-label { font-size: 11px; }
    .pool-actions :global(button), .pool-actions a, .pool-source-promote { min-height: var(--control-height); min-width: var(--control-height); }
    .pool-actions a { display: flex; align-items: center; }
    .pool-source-row { flex-wrap: wrap; }
    .pool-source-url { flex: 1 1 70%; min-width: 0; }
  }
  .pool-title-source {
    margin-left: auto; font-size: 0.72rem; color: #556; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; max-width: 340px;
    text-decoration: none;
  }
  .pool-title-source:hover { color: #88a; text-decoration: underline; }
  .pool-sources-toggle {
    margin-left: 8px; font-size: var(--control-font); background: none; border: 1px solid #334;
    color: #99a; border-radius: var(--control-radius); padding: 0 var(--control-padding-x); cursor: pointer; white-space: nowrap; min-height: var(--control-height); box-sizing: border-box; }
  .pool-sources-toggle:hover { border-color: #557; color: #c9a0ff; }
  .pool-pair-scope {
    margin-left: 8px; font-size: var(--control-font); background: none; border: 1px solid #334;
    color: #99a; border-radius: var(--control-radius); padding: 0 var(--control-padding-x); cursor: pointer; white-space: nowrap; min-height: var(--control-height); box-sizing: border-box; }
  .pool-pair-scope:hover { border-color: #557; color: #c9a0ff; }
  .pool-pair-scope.on { color: #8fd19e; border-color: #2a5a3a; background: #14241a; }
  .pool-pair-scope:disabled { opacity: 0.6; cursor: default; }
  .pool-tagger-exclude {
    margin-left: 8px; font-size: var(--control-font); background: none; border: 1px solid #334;
    color: #99a; border-radius: var(--control-radius); padding: 0 var(--control-padding-x); cursor: pointer; white-space: nowrap; min-height: var(--control-height); box-sizing: border-box; }
  .pool-tagger-exclude:hover { border-color: #557; color: #c9a0ff; }
  .pool-tagger-exclude.on { color: #e08a8a; border-color: #5a2a2a; background: #241414; }
  .pool-tagger-exclude:disabled { opacity: 0.6; cursor: default; }
  .pool-sources-list {
    list-style: none; margin: 2px 0 6px; padding: 6px 10px; background: #14141c;
    border: 1px solid #262633; border-radius: var(--control-radius); font-size: 0.72rem;
    flex-shrink: 0;
  }
  .pool-source-row { display: flex; align-items: center; gap: 8px; padding: 2px 0; }
  .pool-source-row.primary { color: #c9a0ff; }
  .pool-source-pri { width: 1em; text-align: center; color: #c9a0ff; }
  .pool-source-url { color: #7a7aa0; text-decoration: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 420px; }
  .pool-source-url:hover { text-decoration: underline; color: #c9a0ff; }
  .pool-source-url.muted { color: #556; }
  .pool-source-note { margin-left: auto; font-size: 0.66rem; color: #6a8; white-space: nowrap; }
  .pool-source-promote {
    font-size: var(--control-font); background: none; border: 1px solid #345; color: #8ab;
    border-radius: var(--control-radius); padding: 0 var(--control-padding-x); cursor: pointer; white-space: nowrap; min-height: var(--control-height); box-sizing: border-box; }
  .pool-source-promote:hover:not(:disabled) { border-color: #5a7; color: #afe; }
  .pool-source-promote:disabled { opacity: 0.5; cursor: default; }
  /* 検証 chip の描画は PoolVerifyBadge.svelte (chip variant) へ移設。 */
  .pool-tagger-source { font-size: 10.5px; color: var(--muted, #888); font-family: monospace; }
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
