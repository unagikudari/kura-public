<script lang="ts">
  // tagger ダッシュボード — kura 共通 Header + 配信 HTML (iframe)。
  // 中身は /api/tagger-dashboard が DB から生成する HTML。
  // 親と iframe は同じ control-tokens.css を読み、操作部の寸法を共有する。
  import Header from '#lib/components/Header.svelte';
</script>

<svelte:head><title>tagger dashboard — kura</title></svelte:head>

<div class="page">
  <Header />
  <iframe class="dash-frame" src="/api/tagger-dashboard?raw=1&t={Date.now()}" title="tagger dashboard"></iframe>
</div>

<style>
  .page { display: flex; flex-direction: column; height: 100dvh; min-height: 0; background: var(--background); }
  .dash-frame { flex: 1; width: 100%; min-height: 0; border: 0; display: block; background: var(--background); }
</style>
