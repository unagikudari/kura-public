<script lang="ts">
  // タグ一括編集 (grid ?bulk=1) 中、サムネイル内に「target タグと同一カテゴリ」の
  // タグを重畳表示する帯 (2026-07-20 user 要望)。紛れ込んだ同カテゴリの誤タグを
  // グリッドから出ずに 2 タップで即除去できる:
  //   1 タップ = 削除待機 (armed 赤ハイライト、5 秒/外タップ/別チップ/ページ変化で解除)
  //   同じチップをもう 1 タップ = version-aware PATCH で本削除 (409 は再取得リトライ)
  // hover でタグ description をツールチップ表示 (共有 lazy cache #lib/tagDescription)。
  // チップ操作はサムネイルの選択トグルに波及させない (stopPropagation は TagChip
  // 呼び出し側 = ここで担保)。
  import TagChip from '#lib/components/TagChip.svelte';
  import { catColor } from '#lib/categoryColor.ts';
  import { patchTagVersionAware, type PatchDeps } from '#lib/bulkTag.ts';
  import { tagDescription } from '#lib/tagDescription.ts';
  import { tagRelations } from '#lib/tagRelations.ts';
  import { verifyTarget } from '#lib/verify.ts';
  import type { Post } from '#lib/types.ts';
  import { t } from '#lib/i18n/index.ts';
  const m = t();

  let { post, category, deps, onUpdated, pins = null, onToggleLock = null, focusTag = null, stacked = false }: {
    post: Post;
    /** 一括バーの target タグのカテゴリ (解決は呼び手) */
    category: string;
    deps: PatchDeps;
    onUpdated: (p: Post) => void;
    /** この post の pin 来歴マップ {tag: 'present'|'absent'}。帯内の全タグ末尾に
     *  鍵を出す (user 2026-08-02: 錠はタグに付く — 同カテゴリの他タグの状態も
     *  ここで確認・切替できる)。 */
    pins?: Record<string, 'present' | 'absent'> | null;
    /** タグ単位の施錠/解錠 (クリックで状態切替)。 */
    onToggleLock?: ((tag: string) => void) | null;
    /** この帯が代表するフォーカスタグ。複数フォーカス時、🚫 (人間が解除済み)
     *  マークが常にアクティブタグを指してしまうのを防ぐ (既定はアクティブ)。 */
    focusTag?: string | null;
    /** true のとき自身では左上アンカーせず、呼び手が用意した縦積みコンテナの
     *  通常フロー内に収まる (複数フォーカスで帯が2本以上出るとき、全帯が
     *  position:absolute top:0/left:0 で同じ位置に重なっていた — user 報告
     *  2026-08-03「フォーカスタグカテゴリのサムネイル表示が重なってしまっている」)。
     *  既定 false = 従来どおり自分で左上アンカー (viewer の単独呼び出し等)。 */
    stacked?: boolean;
  } = $props();

  const bandTag = $derived(focusTag ?? $verifyTarget);

  const tags = $derived((post.tags ?? []).filter(t => t.category === category));

  // ── target / 関連タグの強調 ──
  // target そのもの = 黄背景、関係 (tag_relation スキーマ) = related 下線 /
  // exclusive 破線下線。関係は target 変更ごとに 1 回だけ API から引く。
  let relMap = $state<Map<string, 'related' | 'exclusive'> | null>(null);
  $effect(() => {
    const t = $verifyTarget;
    relMap = null;
    if (!t) return;
    tagRelations(t).then(m => { if ($verifyTarget === t) relMap = m; }, () => {});
  });
  function emphasisFor(names: string[]): 'target' | 'related' | 'exclusive' | null {
    const t = $verifyTarget;
    if (t && names.includes(t)) return 'target';
    if (relMap) for (const n of names) { const k = relMap.get(n); if (k) return k; }
    return null;
  }

  // ── 2 段階削除 ──
  const ARM_TIMEOUT_MS = 5000;
  let armed = $state<string | null>(null);
  let busy = $state<string | null>(null);
  let armTimer: ReturnType<typeof setTimeout> | null = null;
  function disarm() {
    if (armTimer) { clearTimeout(armTimer); armTimer = null; }
    armed = null;
  }
  $effect(() => { void post.id; disarm(); hideTip(); });
  function onWindowPointerdown(e: PointerEvent) {
    if (armed && rootEl && !rootEl.contains(e.target as Node)) disarm();
  }
  let rootEl = $state<HTMLElement | null>(null);

  async function chipClick(e: MouseEvent, name: string) {
    e.stopPropagation(); // サムネの選択トグルへ波及させない
    e.preventDefault();
    if (busy) return;
    if (armed !== name) {
      if (armTimer) clearTimeout(armTimer);
      armed = name;
      armTimer = setTimeout(() => { armTimer = null; armed = null; }, ARM_TIMEOUT_MS);
      return;
    }
    disarm();
    busy = name;
    try {
      const updated = await patchTagVersionAware(post, ts => ts.filter(n => n !== name), deps);
      onUpdated(updated);
    } catch (err) {
      console.error('thumb tag delete failed', err);
    } finally {
      busy = null;
    }
  }

  // ── hover ツールチップ (description) ──
  // tip は body へ portal する: viewer の .item は contain:layout style paint を
  // 持ち、position:fixed の基準が viewport でなく item になる (+ paint containment
  // で item 外は切り取り) — component 内に置くと「遠く離れた場所に出る/見えない」。
  function portal(node: HTMLElement) {
    document.body.appendChild(node);
    return { destroy() { node.remove(); } };
  }
  type Tip = { name: string; x: number; y: number; desc: string | null | undefined };
  let tip = $state<Tip | null>(null);
  let tipTimer: ReturnType<typeof setTimeout> | null = null;
  function hideTip() {
    if (tipTimer) { clearTimeout(tipTimer); tipTimer = null; }
    tip = null;
  }
  function chipEnter(e: PointerEvent, name: string) {
    if (e.pointerType !== 'mouse') return;
    const el = e.currentTarget as HTMLElement;
    if (tipTimer) clearTimeout(tipTimer);
    tipTimer = setTimeout(() => {
      tipTimer = null;
      if (!el.isConnected) return;
      const r = el.getBoundingClientRect();
      tip = { name, x: Math.max(4, Math.min(r.left, window.innerWidth - 280)),
              y: r.bottom + 4, desc: undefined };
      tagDescription(name).then(
        d => { const t = tip; if (t && t.name === name) tip = { ...t, desc: d }; },
        () => {}, // 失敗は静かに (名前のみのツールチップのまま)
      );
    }, 130);
  }
</script>

<svelte:window onpointerdowncapture={onWindowPointerdown} />

{#if tags.length > 0 || (bandTag && pins?.[bandTag] === 'absent')}
  <div class="tct" class:stacked bind:this={rootEl} role="group" aria-label={m.tags.thumb.groupLabel}>
    {#if bandTag && pins?.[bandTag] === 'absent'}
      <span class="tct-negated" title={m.tags.thumb.negatedTitle(bandTag)}>🚫{bandTag}</span>
    {/if}
    {#each tags as t (t.names[0])}
      {@const name = t.names[0]}
      {@const pin = pins?.[name] ?? null}
      <span class="tct-entry">
        <TagChip localizedNames={t.localizedNames} {name} color={catColor(t.category)} variant="overlay"
          emphasis={emphasisFor(t.names)}
          armed={armed === name} busy={busy === name}
          title={pin === 'present' ? m.tags.thumb.lockedNoDelete
            : armed === name ? m.tags.common.tapAgainToDelete : m.tags.common.tapToArmDelete}
          onclick={pin === 'present' ? null : (e: MouseEvent) => chipClick(e, name)}
          onpointerenter={(e: PointerEvent) => chipEnter(e, name)}
          onpointerleave={hideTip} />
        {#if onToggleLock}
          <button class="tct-lock" class:on={pin === 'present'} type="button"
            title={pin === 'present'
              ? m.tags.thumb.lockedTitle(name)
              : m.tags.thumb.lockTitle(name)}
            onclick={(e) => { e.stopPropagation(); e.preventDefault(); onToggleLock?.(name); }}>{pin === 'present' ? '🔒' : '🔓'}</button>
        {/if}
      </span>
    {/each}
  </div>
{/if}

{#if tip}
  <div class="tct-tip" use:portal style="left:{tip.x}px; top:{tip.y}px">
    <div class="tct-tip-name">{tip.name}</div>
    {#if tip.desc === undefined}
      <div class="tct-tip-desc dim">…</div>
    {:else if tip.desc}
      <div class="tct-tip-desc">{tip.desc}</div>
    {:else}
      <div class="tct-tip-desc dim">{m.tags.common.noDescription}</div>
    {/if}
  </div>
{/if}

<style>
  .tct-entry { display: inline-flex; align-items: baseline; gap: 1px; }
  .tct-lock {
    background: none; border: none; padding: 0 1px; cursor: pointer;
    font-size: 0.72rem; line-height: 1.35;
    text-shadow: 0 0 4px rgba(0, 0, 0, 0.95);
    opacity: 0.45; filter: grayscale(1);
  }
  .tct-lock:hover, .tct-lock.on { opacity: 1; filter: none; }
  .tct-negated {
    font-size: 0.76rem; line-height: 1.35; white-space: nowrap;
    color: #e08a8a; font-family: monospace;
    text-shadow: 0 0 4px rgba(0, 0, 0, 0.95), 0 1px 2px rgba(0, 0, 0, 0.9);
  }

  .tct {
    /* 左上アンカー (2026-07-20 user 指定: 一括編集の関連タグはサムネ左上、既定)。
       stacked=true (呼び手が縦積みコンテナを用意) では下の .stacked が
       position/max-width を上書きし、通常フロー要素として振る舞う — 複数
       フォーカスで全帯が top:0/left:0 に重なっていた regression (user 報告
       2026-08-03「フォーカスタグカテゴリのサムネイル表示が重なってしまっている」)
       の修正。背景・パディング等の見た目は両方で共通。 */
    position: absolute; left: 0; top: 0; z-index: 3;
    display: flex; flex-wrap: wrap; gap: 2px 4px;
    padding: 3px 4px;
    background: rgba(0, 0, 0, 0.45);
    border-bottom-right-radius: 6px;
    pointer-events: auto;
    font-size: 0.68rem;
    /* 右上のバッジ域 (カバー/★/POOL チップ) と重ならないよう手前で改行 */
    max-width: 55%; max-height: 45%; overflow-y: auto;
    /* overscroll-behavior: contain は撤去 (2026-08-02): スクロール余地の無い
       空のスクロールコンテナでも wheel の親への伝播を遮断するため、帯に
       ポインタが乗るとページがスクロール不能になっていた (user 報告)。
       余地がある帯は自身を先にスクロールし、端で通常どおり親へ連鎖する。 */
  }
  .tct.stacked {
    position: static; left: auto; top: auto;
    max-width: none; max-height: none; overflow-y: visible;
  }
  .tct-tip {
    position: fixed; z-index: 50; max-width: 270px;
    background: rgba(10, 10, 14, 0.92); border: 1px solid rgba(255, 255, 255, 0.18);
    border-radius: 5px; padding: 6px 9px; pointer-events: none;
    font-size: 0.72rem; line-height: 1.35;
  }
  .tct-tip-name { font-weight: 700; margin-bottom: 2px; }
  .tct-tip-desc { color: #cfcfd6; white-space: pre-wrap; }
  .tct-tip-desc.dim { color: #8a8a92; font-style: italic; }
</style>
