<script lang="ts">
  // 子の mount をブラウザのアイドル時間へ遅延する薄いラッパー。
  // 用途: 一括編集のサムネ帯 (ThumbCategoryTags)。スクロールで新規行が入る
  // たびに数十 DOM ノード×行分を同一フレームで構築するとつっかえ、逆に
  // isScrolling で {#if} を切ると開始/停止のたびに全帯が点滅する (user 報告
  // 2026-08-02「タグがチラつく」)。ここでは「一度出たら消さない・出すのは
  // idle 時」— requestIdleCallback (無い環境は setTimeout 50ms) で 1 帯ずつ
  // フレームの隙間に mount する。timeout でアイドルが来ない場合も 300ms で保証。
  import { onMount } from 'svelte';
  import type { Snippet } from 'svelte';

  let { children }: { children: Snippet } = $props();
  let show = $state(false);

  onMount(() => {
    const w = window as any;
    if (typeof w.requestIdleCallback === 'function') {
      const h = w.requestIdleCallback(() => { show = true; }, { timeout: 300 });
      return () => w.cancelIdleCallback?.(h);
    }
    const t = setTimeout(() => { show = true; }, 50);
    return () => clearTimeout(t);
  });
</script>

{#if show}{@render children()}{/if}
