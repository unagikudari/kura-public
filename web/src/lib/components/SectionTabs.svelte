<script lang="ts">
  let { label, tabs }: { label: string; tabs: { label: string; href: string; active?: boolean }[] } = $props();
</script>
<nav class="section-tabs" aria-label={label}>
  {#each tabs as tab}<a href={tab.href} class:active={tab.active} aria-current={tab.active ? 'page' : undefined}>{tab.label}</a>{/each}
</nav>
<style>
  .section-tabs { display:flex; flex-shrink:0; gap:4px; padding:0 8px; overflow-x:auto; overscroll-behavior-x:contain; scrollbar-width:thin; border-bottom:1px solid var(--line); background:var(--background); }
  a { display:flex; align-items:center; flex-shrink:0; min-height:var(--control-height); padding:4px 10px; box-sizing:border-box; border-bottom:3px solid transparent; color:var(--muted); text-decoration:none; font-size:13px; }
  a.active { color:var(--text); border-bottom-color:var(--accent); font-weight:600; }
</style>
