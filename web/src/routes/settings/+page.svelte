<script lang="ts">
  import { getContext } from 'svelte';
  import type { Writable } from 'svelte/store';
  import Header from '#lib/components/Header.svelte';
  import { IDENTITY_CATEGORIES, IDENTITY_DISPLAY_CONTEXT, IDENTITY_DISPLAY_COOKIE, readIdentityDisplay, type IdentityDisplay } from '#lib/identityDisplay.ts';
  import { t } from '#lib/i18n/index.ts';
  const m = t();
  const sm = m.pages.settings;
  const preferences = getContext<Writable<IdentityDisplay>>(IDENTITY_DISPLAY_CONTEXT);
  function save(next: IdentityDisplay) {
    const checked = readIdentityDisplay(JSON.stringify(next));
    preferences.set(checked);
    document.cookie = `${IDENTITY_DISPLAY_COOKIE}=${encodeURIComponent(JSON.stringify(checked))}; Path=/; Max-Age=31536000; SameSite=Lax`;
  }
  function toggle(key: 'viewer' | 'artist' | 'character' | 'copyright', event: Event) {
    save({ ...$preferences, [key]: (event.target as HTMLInputElement).checked });
  }
</script>

<svelte:head><title>{sm.title}</title></svelte:head>
<div class="settings-page">
  <Header />
  <main>
    <a class="back" href="/?q=">{sm.back}</a>
    <h1>{sm.heading}</h1>
    <p><a href="/settings/backup">{sm.backupLink}</a></p>
    <p class="intro">{sm.intro}</p>
    <section aria-labelledby="identity-settings-title">
      <h2 id="identity-settings-title">{m.pages.identity.heading}</h2>
      <fieldset><legend>{sm.where}</legend>
        <label><span>{sm.viewerLabel}</span><input type="checkbox" checked={$preferences.viewer} onchange={(event) => toggle('viewer', event)} /></label>
      </fieldset>
      <fieldset><legend>{sm.what}</legend>
        {#each IDENTITY_CATEGORIES as category}
          <label><span>{m.pages.identity[category.key]}</span><input type="checkbox" checked={$preferences[category.key]} onchange={(event) => toggle(category.key, event)} /></label>
        {/each}
      </fieldset>
      <label class="limit"><span>{sm.perCategory}</span>
        <select aria-label={sm.perCategory} value={$preferences.namesPerCategory} onchange={(event) => save({ ...$preferences, namesPerCategory: Number((event.target as HTMLSelectElement).value) as IdentityDisplay['namesPerCategory'] })}>
          {#each [1, 2, 3, 5] as count}<option value={count}>{sm.countN(count)}</option>{/each}
        </select>
      </label>
      <p class="hint">{sm.hint}</p>
      <button class="reset" type="button" onclick={() => save(readIdentityDisplay())}>{sm.reset}</button>
    </section>
  </main>
</div>

<style>
  .settings-page { height: 100dvh; overflow-y: auto; background: var(--background); }
  main { max-width: 640px; margin: 0 auto; padding: 6px max(8px, env(safe-area-inset-left)) max(8px, env(safe-area-inset-bottom)); }
  .back { color: var(--muted); text-decoration: none; font-size: 13px; }
  h1 { margin: 6px 0 4px; font-size: 20px; font-weight: 600; letter-spacing: -.025em; }
  .intro, .hint { color: var(--muted); font-size: 13px; line-height: 1.4;  margin: 4px 0; }
  section { margin-top: 6px; padding: 6px; border: 1px solid var(--line); border-radius: var(--control-radius); background: var(--surface); }
  h2 { margin: 0 0 6px; font-size: 16px; font-weight: 600; }
  fieldset { padding: 0; margin: 0 0 6px; border: 0; }
  legend { margin-bottom: 2px; font-size: 12px; color: var(--accent); }
  label { display: flex; align-items: center; justify-content: space-between; gap: var(--control-gap); min-height: var(--control-height); font-size: 13px; cursor: pointer; }
  input { width: 20px; height: 20px; accent-color: var(--accent); }
  select { min-height: var(--control-height); padding: 0 var(--control-padding-x); font-size: 16px; color: var(--text); background: var(--input); border: 1px solid var(--line); border-radius: var(--control-radius);  box-sizing: border-box; }
  .reset { min-height: var(--control-height); padding: 0 var(--control-padding-x); margin-top: 8px; border: 1px solid var(--line); border-radius: var(--control-radius); background: transparent; color: var(--text); cursor: pointer;  box-sizing: border-box; }
  @media (max-width: 420px) { main { padding-top: 18px;  padding: 6px max(8px, env(safe-area-inset-left)) max(8px, env(safe-area-inset-bottom)); } section { padding: 6px;  margin-top: 6px; } .limit { flex-wrap: wrap; gap: var(--control-gap); } }
</style>
