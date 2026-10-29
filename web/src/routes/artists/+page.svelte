<script lang="ts">
  import Header from '#lib/components/Header.svelte';
  import SectionTabs from '#lib/components/SectionTabs.svelte';
  import { invalidateAll } from '$app/navigation';
  import { addTagLink, deleteTagLink, updateTagLink, mediaUrl } from '#lib/api.ts';
  import { artistIntegration, artistEditHref, externalUrl, type ArtistSource } from '#lib/artistWorkspace.ts';
  import type { TagDetail } from '#lib/types.ts';
  import type { PageData } from './$types';
  import { t } from '#lib/i18n/index.ts';
  const m = t();
  const a = m.pages.artists;
  let { data }: { data: PageData } = $props();
  let busy = $state<string | null>(null);
  let message = $state('');
  let receiptHref = $state<string | null>(null);
  let editing = $state<number | null>(null);
  let newUrl = $state('');
  let platform = $state('');
  let patrolUrl = $state('');
  const artists = $derived(data.tags?.results ?? []);
  const sources = $derived(data.sources?.sources ?? []);
  const unassigned = $derived(sources.filter(s => s.role !== 'history' && !s.artistIds.length));
  const linksFor = (id: number) => data.links.filter(l => l.tagId === id);
  const sourcesFor = (id: number) => sources.filter(s => s.role !== 'history' && s.artistIds.includes(id));
  function historyFor(id: number) {
    const groups = new Map<string, typeof sources>();
    for (const source of sources) {
      if (source.role !== 'history' || !source.artistIds.includes(id)) continue;
      const url = source.canonicalUrl || source.url;
      const group = groups.get(url) ?? [];
      group.push(source); groups.set(url, group);
    }
    return [...groups].map(([url, rows]) => ({ url, rows }));
  }
  const time = (value: string | null) => value ? new Date(value).toLocaleString('ja-JP', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : a.unconfirmed;
  const coreKind = (source: ArtistSource) => source.kind === 'search' || source.kind === 'creator';
  const unverified = (source: ArtistSource) => artistIntegration?.describe?.(source)?.unverified ?? (!coreKind(source) && source.active && !source.lastAt);
  const kindLabel = (source: ArtistSource) => artistIntegration?.describe?.(source)?.label ?? (source.kind === 'search' ? a.kindSearch : a.kindAccount);
  const label = (source: ArtistSource) => unverified(source) ? a.unconfirmed : source.state === 'partial' ? a.statePartial : source.state === 'dead' ? a.stateDead : source.state === 'walking' ? a.stateWalking : source.active ? a.stateActive : a.stateStopped;
  function location(raw: string) { try { const u = new URL(raw); return u.host + u.pathname + u.search; } catch { return raw; } }
  async function act(key: string, work: () => Promise<unknown>) {
    if (busy) return;
    busy = key; message = ''; receiptHref = null;
    try { const result = await work(); receiptHref = artistIntegration?.receiptHref?.(result) ?? null; await invalidateAll(); }
    catch (error) { message = error instanceof Error ? error.message : a.updateFailed; }
    finally { busy = null; }
  }
  async function attach(artist: TagDetail, url: string, explicitPlatform = '') {
    const valid = externalUrl(url);
    if (!valid) throw new Error(a.needHttpUrl);
    const host = new URL(valid).hostname.replace(/^www\./, '');
    const reference = artistIntegration ? await artistIntegration.reference(url.trim()) : { url:url.trim(), platform:host };
    await addTagLink(artist.names[0], { url: reference.url, platform: explicitPlatform.trim() || reference.platform || host });
    editing = null; newUrl = ''; platform = '';
  }
  function href(page: number) { const p = new URLSearchParams({ q: data.query, page: String(page) }); if (data.assignUrl) p.set('source', data.assignUrl); return '/artists?' + p; }
</script>

<svelte:head><title>{a.title}</title></svelte:head>
<div class="page">
<Header showSearch={false} />
<SectionTabs label={a.sectionLabel} tabs={[
  {label:a.tabArtists,href:'/artists',active:data.view === 'artists'},
  {label:a.tabUnassigned(unassigned.length + data.unclassified.length),href:'/artists?view=unassigned',active:data.view === 'unassigned'},
  ...(artistIntegration?.advancedHref ? [{label:a.tabAdvanced,href:artistIntegration.advancedHref}] : [])
]} />
<div class="artist-scroll">
<main>
  <div class="toolbar">
    <div><h1>{a.heading}</h1>{#if data.tags}<span class="muted">{a.artistCount(data.tags.total?.toLocaleString() ?? '')}</span>{/if}</div>
    {#if data.id}<a class="advanced" href="/artists">{a.backToList}</a>{/if}
    <form action="/artists" method="GET" class="search">
      <input type="search" name="q" value={data.query} placeholder={a.searchPlaceholder} aria-label={a.searchLabel} />
      {#if data.assignUrl}<input type="hidden" name="source" value={data.assignUrl} />{/if}
      <button type="submit">{a.search}</button>
    </form>
    <a class="button" href="/tags/edit?cat=artist&from=%2Fartists">{a.addArtistTag}</a>
    <button disabled={busy !== null} onclick={() => act('refresh', () => Promise.resolve())} aria-label={a.refresh}>↻</button>
  </div>
  {#if receiptHref}<p class="receipt" role="status"><a href={receiptHref}>{a.receipt}</a></p>{/if}
  {#if message}<p class="error" role="alert">{message}</p>{/if}
  {#if data.catalogError}<p class="error" role="alert">{data.catalogError}</p>{/if}
  {#if data.linksError}<p class="error" role="alert">{data.linksError}</p>{/if}
  {#if data.unclassifiedError}<p class="error" role="alert">{data.unclassifiedError}</p>{/if}
  {#if data.linksLimited}<p class="muted">{a.linksLimited}</p>{/if}
  {#if data.sourcesError}<p class="error" role="alert">{data.sourcesError}</p>{/if}
  {#if data.sources?.limited}<p class="muted">{a.sourcesLimitedBefore}{#if artistIntegration?.listHref}<a href={artistIntegration.listHref}>{a.patrolList}</a>{:else}{a.patrolList}{/if}{a.sourcesLimitedAfter}</p>{/if}
  {#if data.assignUrl && data.view === 'artists'}
    <aside class="assignment"><strong>{a.assignHeading}</strong><span>{location(data.assignUrl)}</span><a href="/artists">{a.assignCancel}</a></aside>
  {/if}
  {#if data.view === 'unassigned'}
    <p class="muted">{artistIntegration ? a.unassignedIntroIntegration : a.unassignedIntroPlain}</p>
    {#if artistIntegration}<form class="patrol-add" onsubmit={(event) => { event.preventDefault(); act('add-patrol', async () => { const value = externalUrl(patrolUrl); if (!value) throw new Error(a.checkUrl); await artistIntegration!.register(value); patrolUrl = ''; }); }}><input type="url" required disabled={busy !== null} bind:value={patrolUrl} placeholder={a.patrolPlaceholder} aria-label={a.patrolUrlLabel} /><button disabled={busy !== null}>{a.registerPatrol}</button></form>{/if}
    {#each data.unclassified as link (link.id)}
      <article class="unassigned"><div class="grow"><strong>{link.tagName}</strong><small>{link.platform} · {link.tagCategory || a.noCategory}</small><small>{location(link.url)}</small></div><a class="button" href={'/tags/edit?' + new URLSearchParams({ tagId:String(link.tagId), q:link.tagName, from:'/artists?view=unassigned' })}>{a.reviewArtistTag}</a></article>
    {/each}
    <section class="source-list">
      {#each unassigned as source (source.key)}
        <article class="unassigned">
          {#if source.cover}<img src={mediaUrl(source.cover)} alt="" loading="lazy" />{/if}
          <div class="grow"><strong>{source.name}</strong><small>{location(source.url)}</small><span class="status" class:attention={source.state === 'dead' || unverified(source)}>{label(source)}</span></div>
          <a class="button" href={'/artists?' + new URLSearchParams({ source: source.url })}>{a.linkToArtist}</a>
          {#if artistIntegration}<button disabled={busy !== null} onclick={() => act(source.key, () => artistIntegration!.setActive(source, !source.active))}>{source.active ? a.stop : a.resume}</button>{/if}
        </article>
      {:else}<p class="empty">{a.noUnassigned}</p>{/each}
    </section>
  {:else}
    <section class="artists" aria-label={a.tabArtists}>
      {#each artists as artist (artist.id)}
        {@const links = linksFor(artist.id)}
        {@const feeds = sourcesFor(artist.id)}
        {@const history = historyFor(artist.id)}
        <article class="artist" id={'artist-' + artist.id}>
          <div class="artist-head">
            <a class="portrait" href={`/?q=${encodeURIComponent(artist.names[0])}`} aria-label={a.imagesOf(artist.names[0])}>
              {#if artist.thumbnailUrl}<img src={mediaUrl(artist.thumbnailUrl)} alt="" loading="lazy" />{:else}<span>◇</span>{/if}
            </a>
            <div class="grow"><h2>{artist.names[0]}</h2>{#if artist.names.length > 1}<p class="aliases">{artist.names.slice(1).join(' · ')}</p>{/if}<small>{a.counts(artist.usages.toLocaleString(), links.length, feeds.length)}</small></div>
            <a class="edit" href={artistEditHref(artist)} aria-label={a.editLabel(artist.names[0])}>{a.edit}</a>
          </div>
          <div class="artist-actions">
            <a href={`/?q=${encodeURIComponent(artist.names[0])}`}>{a.images}</a>
            <a href={`/?mode=pools&q=${encodeURIComponent('tag:' + artist.names[0])}`}>Pool</a>
            {#if data.assignUrl}<button disabled={busy !== null} onclick={() => act('artist:' + artist.id, () => attach(artist, data.assignUrl))}>{a.linkThisArtist}</button>
            {:else}<button onclick={() => { editing = editing === artist.id ? null : artist.id; newUrl = ''; platform = ''; }}>{artistIntegration ? a.addExternalWithPatrol : a.addExternal}</button>{/if}
          </div>
          {#if editing === artist.id}
            <form class="add-link" onsubmit={(event) => { event.preventDefault(); act('artist:' + artist.id, () => attach(artist, newUrl, platform)); }}>
              <input type="url" bind:value={newUrl} disabled={busy !== null} required placeholder={a.linkUrlPlaceholder} aria-label={a.linkUrlLabel} />
              <input disabled={busy !== null} bind:value={platform} placeholder={a.sitePlaceholder} aria-label={a.siteLabel} />
              <button disabled={busy !== null}>{a.link}</button>
            </form>
          {/if}
          <div class="accounts">
            {#each links as link (link.id)}
              <div class="account">
                {#if link.poolCover}<a href={`/?q=pool:${link.poolId}`} class="cover"><img src={mediaUrl(link.poolCover)} alt="" loading="lazy" /></a>{/if}
                <div class="grow"><span class="platform">{link.platform}</span>
                  {#if externalUrl(link.url)}<a class="account-url" href={externalUrl(link.url)!} target="_blank" rel="noopener noreferrer">{link.handle || location(link.url)} ↗</a>{:else}<span>{link.handle || link.url}</span>{/if}
                  {#if link.poolId}<a class="pool-link" href={`/?q=pool:${link.poolId}`}>{link.poolName || 'Pool'}</a>{/if}
                </div>
                {#if artistIntegration && data.sources && !feeds.some(s => s.linkIds.includes(link.id)) && !artistIntegration.registered?.(link)}
                  <button disabled={busy !== null} onclick={() => act('link:' + link.id, () => artistIntegration!.register(link.url))}>{a.registerPatrol}</button>
                {/if}
                <label class="verified" title={a.verified}><input type="checkbox" checked={link.verified} disabled={busy !== null} aria-label={a.verifiedLabel(link.platform)} onchange={(event) => act('link:' + link.id, () => updateTagLink(link.id, { verified:event.currentTarget.checked }))} />✓</label>
                <select aria-label={a.censorLabel(link.platform)} value={String(link.uncensored ?? '')} disabled={busy !== null} onchange={(event) => { const value = event.currentTarget.value; act('link:' + link.id, () => updateTagLink(link.id, { uncensored:value === '' ? null : Number(value) as 0 | 1 })); }}><option value="">{a.unknown}</option><option value="0">{a.censored}</option><option value="1">{a.uncensored}</option></select>
                <button class="unlink" disabled={busy !== null} aria-label={a.unlinkLabel(link.platform)} title={a.unlinkTitle} onclick={() => act('link:' + link.id, () => deleteTagLink(link.id))}>×</button>
              </div>
            {/each}
          </div>
          {#each feeds as source (source.key)}
            <div class="feed">
              <div class="grow"><span class="status" class:attention={source.state === 'dead' || unverified(source)}>{label(source)}</span><span>{kindLabel(source)}</span><small>{a.schedule(time(source.lastAt), source.nextAt ? time(source.nextAt) : null)}</small>{#if source.error}<small class="error">{source.error}</small>{/if}</div>
              {#if artistIntegration}<button disabled={busy !== null || (source.kind === 'search' && !source.active)} onclick={() => act(source.key, () => artistIntegration!.check(source))}>{a.checkNow}</button><button disabled={busy !== null} onclick={() => act(source.key, () => artistIntegration!.setActive(source, !source.active))}>{source.active ? a.stop : a.resume}</button>{/if}
            </div>
          {/each}
          {#if history.length}
            <details class="source-history">
              <summary>{a.history} <span>{a.historyCount(history.length)}</span></summary>
              <ul>{#each history as group (group.url)}
                <li>
                  {#if externalUrl(group.url)}<a href={externalUrl(group.url)!} target="_blank" rel="noopener noreferrer">{location(group.url)} ↗</a>{/if}
                  {#if group.rows[0].poolId}<a class="history-pool" href={`/?q=pool:${group.rows[0].poolId}`}>Pool</a>{/if}
                  {#if group.rows.some(s => s.active)}<small class="attention">{a.activeInHistory}</small>{/if}
                  {#if group.rows.length > 1}<details class="source-aliases"><summary>{a.registeredUrls(group.rows.length)}</summary><ul>{#each group.rows as row (row.key)}<li>{#if externalUrl(row.url)}<a href={externalUrl(row.url)!} target="_blank" rel="noopener noreferrer">{location(row.url)} ↗</a>{/if}</li>{/each}</ul></details>{/if}
                </li>
              {/each}</ul>
            </details>
          {/if}
          {#if busy === 'artist:' + artist.id || links.some(l => busy === 'link:' + l.id) || feeds.some(s => busy === s.key)}<span class="spinner" role="status" aria-label={a.updating}></span>{/if}
          {#if !links.length && !feeds.length && !history.length}<p class="quiet">{a.canLink}</p>{/if}
        </article>
      {:else}<p class="empty">{a.noArtists}</p>{/each}
    </section>
    <nav class="pagination" aria-label={a.pagination}>{#if data.current > 1}<a class="button" href={href(data.current - 1)}>{a.prev}</a>{/if}<span>{data.current}</span>{#if data.current * 60 < (data.tags?.total ?? 0)}<a class="button" href={href(data.current + 1)}>{a.next}</a>{/if}</nav>
  {/if}
</main>
</div>
</div>

<style>
.page{height:100dvh;display:flex;flex-direction:column;min-height:0;overflow:hidden}
.artist-scroll{flex:1;min-height:0;overflow-y:auto;overscroll-behavior:contain}
  main{max-width:1500px;margin:auto;padding:1.1rem 1.4rem 3rem;color:var(--text,#ddd)}
h1{font-size:1.35rem;margin:0}
h2{font-size:1rem;margin:0;overflow-wrap:anywhere}
a{color:inherit;text-decoration:none}
a:hover{text-decoration:underline}
button,.button{border:1px solid var(--line);background:var(--surface-raised);color:inherit;border-radius:6px;padding:.4rem .65rem;font:inherit;font-size:.8rem;cursor:pointer;white-space:nowrap}
button:disabled{opacity:.5;cursor:wait}
.toolbar{display:flex;gap:.8rem;align-items:center;flex-wrap:wrap;margin-bottom:1rem}
.toolbar>div{display:flex;align-items:baseline;gap:.65rem}
.search{display:flex;flex:1;gap:.4rem;min-width:180px;max-width:430px;margin-left:auto}
input{min-width:0;background:var(--input);color:inherit;border:1px solid var(--line);border-radius:6px;padding:.5rem;font:inherit}
.search input{flex:1}
.artists{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:.9rem}
.artist{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:.85rem;min-width:0;position:relative}
.artist-head{display:flex;gap:.75rem;align-items:center}
.portrait{width:66px;height:78px;flex:none;background:var(--surface-raised);border-radius:6px;overflow:hidden;display:grid;place-items:center}
.portrait img,.cover img{width:100%;height:100%;object-fit:cover}
.portrait span{opacity:.4;font-size:1.5rem}
.grow{flex:1;min-width:0}
.aliases{font-size:.75rem;color:var(--muted);margin:.2rem 0;overflow-wrap:anywhere}
small,.muted{font-size:.76rem;color:var(--muted)}
small{display:block}
.edit{font-size:.8rem;color:var(--accent);flex:none}
.artist-actions{display:flex;align-items:center;gap:.8rem;padding:.7rem 0;font-size:.8rem}
.artist-actions button{margin-left:auto}
.accounts{display:grid;gap:.3rem}
.account{display:flex;gap:.55rem;align-items:center;background:var(--surface-raised);border-radius:6px;padding:.45rem}
.cover{height:46px;width:38px;flex:none;overflow:hidden;border-radius:4px}
.platform{font-size:.7rem;color:var(--accent);display:block}
.account-url{font-size:.78rem;overflow:hidden;text-overflow:ellipsis;display:block;white-space:nowrap}
.pool-link{font-size:.7rem;color:#aaa;display:block}
.unlink{border:0;background:none;font-size:1.1rem;padding:.15rem .3rem;color:#8e93a4}
.feed{display:flex;align-items:center;gap:.35rem;border-top:1px solid var(--line);margin-top:.45rem;padding-top:.5rem;font-size:.75rem}
.feed button{font-size:.72rem;padding:.3rem .45rem}
.status{display:inline-block;color:#9dbfa6;font-size:.7rem;margin-right:.5rem}
.attention,.error{color:#efb7a9}
.quiet{font-size:.75rem;color:#717786;margin:.3rem 0}
.add-link{display:flex;flex-wrap:wrap;gap:.4rem;margin-bottom:.7rem}
.add-link input:first-child{flex:1 1 100%;font-size:.8rem}
.add-link input{flex:1;width:100px;font-size:.8rem}
.assignment{display:flex;align-items:center;gap:1rem;padding:.8rem;background:var(--accent-soft);border-radius:7px;margin-bottom:1rem;font-size:.85rem}
.assignment span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.assignment a{flex:none}
.source-list{display:grid;gap:.5rem}
.unassigned{display:flex;align-items:center;gap:.7rem;padding:.8rem;background:var(--surface);border:1px solid var(--line);border-radius:8px}
.unassigned img{width:48px;height:55px;object-fit:cover;border-radius:4px}
.unassigned strong,.unassigned small{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pagination{display:flex;justify-content:center;align-items:center;gap:1rem;margin-top:1.3rem}
.empty{padding:2rem;text-align:center;color:#999}
.spinner{position:absolute;right:.65rem;bottom:.65rem;width:12px;height:12px;border:2px solid #50576c;border-top-color:#d6deff;border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}
}
@media(max-width:600px){main{padding:.8rem}
.artists{grid-template-columns:1fr}
.search{order:2;max-width:none;flex-basis:100%;margin:0}
.toolbar{gap:.6rem}
.toolbar>.button{margin-left:auto}
.unassigned{flex-wrap:wrap}
.unassigned>.grow{flex-basis:60%}
.feed{flex-wrap:wrap}
.feed>.grow{flex-basis:100%}
}

.verified{display:flex;align-items:center;gap:.2rem;font-size:.7rem;color:var(--muted)}
.verified input{width:12px;height:12px;margin:0}
.account select{max-width:68px;background:var(--surface);color:var(--muted);border:1px solid var(--line);border-radius:4px;font-size:.7rem;padding:.2rem}
.advanced{font-size:.75rem;color:var(--muted)}
.patrol-add{display:flex;gap:.5rem;max-width:650px;margin-bottom:1rem}
.patrol-add input{flex:1}
.artist h2{display:-webkit-box;-webkit-line-clamp:2;line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.accounts .account{flex-wrap:wrap}
.accounts .account>.grow{min-width:90px}

.source-history{border-top:1px solid var(--line);margin-top:.6rem;padding-top:.5rem;font-size:.75rem;color:var(--muted)}
.source-history summary{cursor:pointer}.source-history summary span{margin-left:.4rem}.source-history ul{list-style:none;margin:.5rem 0;padding-left:.6rem}.source-history li{margin:.5rem 0;overflow-wrap:anywhere}.history-pool{color:var(--accent);margin-left:.6rem}.source-aliases{font-size:.7rem;margin-top:.25rem}
</style>
