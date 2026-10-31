<script lang="ts">
  import Header from '#lib/components/Header.svelte';
  import { t } from '#lib/i18n/index.ts';
  const m = t();
  const om = m.pages.operator;
  type Person = { id: number; name: string; rank: string };
  type Source = 'env' | 'empty' | 'unset';
  type Row = { name: string; source: Source; secret: boolean; withheld: boolean; value: string | null };
  let { data } = $props();
  const body = $derived(data.data as { owner: { id: number; source: Source; user: Person | null }; administrators: Person[]; settings: Row[] } | null);
  const shown = (r: Row) => r.source !== 'env' ? om.unset : r.secret ? om.secretSet : r.withheld ? om.withheld : r.value;
  const from = (s: Source) => s === 'env' ? om.fromEnv : s === 'empty' ? om.fromEmpty : om.fromUnset;
</script>
<svelte:head><title>{om.title}</title></svelte:head>
<div class="page"><Header /><main>
  <a class="back" href="/settings">{om.back}</a>
  <h1>{om.heading}</h1>
  {#if data.state === 'forbidden'}<p class="notice">{om.forbidden}</p>
  {:else if data.state === 'failed' || !body}<p class="notice">{om.failed}</p>
  {:else}
    <p class="intro">{om.intro}</p>
    <section aria-label={om.owner}>
      <h2>{om.owner}</h2>
      <p class="owner">{#if body.owner.user}#{body.owner.user.id} {body.owner.user.name} <span class="muted">({body.owner.user.rank})</span>{:else}<span class="notice">{om.ownerMissing(body.owner.id)}</span>{/if}
        <span class="src">{from(body.owner.source)}</span></p>
      <h2>{om.administrators}</h2>
      <ul>{#each body.administrators as a}<li>#{a.id} {a.name}{#if a.id === body.owner.id} <span class="src">owner</span>{/if}</li>{/each}</ul>
    </section>
    <section aria-label={om.settings}>
      <h2>{om.settings}</h2>
      <dl>{#each body.settings as r}<dt>{r.name}</dt><dd><span class:muted={r.source !== 'env'}>{shown(r)}</span> <span class="src">{from(r.source)}</span></dd>{/each}</dl>
    </section>
  {/if}
</main></div>
<style>
  .page { height:100dvh;overflow-y:auto;background:var(--background); }main { max-width:760px;margin:0 auto;padding:16px; }.back { color:var(--muted);font-size:12px; }h1 { font-size:22px;margin:12px 0; }h2 { font-size:15px;margin:4px 0 10px; }
  section { border:1px solid var(--line);border-radius:8px;background:var(--surface);padding:18px;margin-bottom:16px; }.intro,.muted { color:var(--muted);font-size:13px;line-height:1.6; }.notice { color:#e0be82;font-size:13px; }
  ul { margin:0 0 4px;padding-left:18px;font-size:14px;line-height:1.8; }.owner { font-size:14px;margin:0 0 16px; }
  dl { display:grid;grid-template-columns:minmax(0,260px) minmax(0,1fr);gap:8px 12px;margin:0;font-size:13px; }dt { color:var(--muted);font-family:ui-monospace,monospace;overflow-wrap:anywhere; }dd { margin:0;overflow-wrap:anywhere; }
  .src { font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:0 5px;margin-left:4px; }
  @media(max-width:520px) { main { padding:12px; }section { padding:14px; }dl { grid-template-columns:1fr;gap:2px; }dd { margin-bottom:8px; } }
</style>
