<script lang="ts">
  import { onMount } from 'svelte';
  import { browser } from '$app/env';
  import { goto } from '$app/navigation';
  import Header from '#lib/components/Header.svelte';
  import {
    mediaUrl,
    listCensorAnnotations,
    createCensorAnnotation,
    deleteCensorAnnotation,
    getCensorReview,
    putCensorReview,
  } from '#lib/api.ts';
  import { t } from '#lib/i18n/index.ts';

  const m = t();
  const cm = m.pages.censor;

  let { data } = $props();
  const post = data.post;

  const CENSOR_TYPES = ['mosaic', 'white_bar', 'black_bar', 'slash', 'other'];
  const TOOLS = ['rect', 'polygon', 'pen'] as const;
  const TOOL_MARK: Record<(typeof TOOLS)[number], string> = { rect: '▭', polygon: '⬠', pen: '✎' };
  const TOOL_NAME: Record<(typeof TOOLS)[number], string> = { rect: cm.toolRect, polygon: cm.toolPolygon, pen: cm.toolPen };
  const COLORS: Record<string, string> = {
    mosaic: '#e84393', white_bar: '#0984e3', black_bar: '#00b894',
    slash: '#fdcb6e', other: '#b2bec3',
  };
  const ERASE_COLOR = '#ff1744';   // eraser regions render red+dashed (they SUBTRACT)

  // Persist tool / censor type / pen width across pages — annotating many pages
  // of one pool, the settings should carry over (localStorage, browser-only).
  const _ls = (k: string) => (browser ? localStorage.getItem(k) : null);
  const _savedTool = _ls('kura_censor_tool');
  const _savedType = _ls('kura_censor_type');
  const _savedPenW = parseFloat(_ls('kura_censor_penw') ?? '');

  let annotations = $state<any[]>([]);
  let review = $state<any>({ state: 'unreviewed', hasNoCensor: false });
  let tool = $state<(typeof TOOLS)[number]>(
    (TOOLS as readonly string[]).includes(_savedTool ?? '') ? (_savedTool as any) : 'rect');
  let censorType = $state(CENSOR_TYPES.includes(_savedType ?? '') ? (_savedType as string) : 'mosaic');
  let eraseMode = $state(false);   // when on, drawn shapes are negative (subtract from the mask)
  let status = $state('');
  let selected = $state<number | null>(null);
  let penWidth = $state(_savedPenW > 0 ? _savedPenW : 0.025);   // normalized to image width
  let dispW = $state(1);          // displayed image width (px) for on-screen stroke
  const PEN_WIDTHS = [{ k: cm.penXs, v: 0.004 }, { k: cm.penS, v: 0.012 }, { k: cm.penM, v: 0.025 }, { k: cm.penL, v: 0.05 }];
  let svgEl: SVGSVGElement;

  // remember the chosen tool / type / pen width for the next page of the pool
  $effect(() => { if (browser) localStorage.setItem('kura_censor_tool', tool); });
  $effect(() => { if (browser) localStorage.setItem('kura_censor_type', censorType); });
  $effect(() => { if (browser) localStorage.setItem('kura_censor_penw', String(penWidth)); });

  // in-progress drawing
  let draft = $state<any>(null);
  let poly = $state<number[][]>([]);

  // Displayed image width drives the on-screen pen stroke width. Measure it
  // robustly — on mount, on image load, and on any resize — so a freshly drawn
  // pen stroke is never invisible (the bug: stroke-width = sw * dispW ≈ 0 until
  // a scroll/resize fired the observer).
  function measureDispW() { if (svgEl && svgEl.clientWidth) dispW = svgEl.clientWidth; }

  onMount(() => {
    (async () => {
      try {
        [annotations, review] = await Promise.all([
          listCensorAnnotations(post.id),
          getCensorReview(post.id),
        ]);
      } catch (e: any) { status = 'load error: ' + e.message; }
    })();
    measureDispW();
    requestAnimationFrame(measureDispW);   // after first layout
    const ro = new ResizeObserver(measureDispW);
    if (svgEl) ro.observe(svgEl);
    return () => ro.disconnect();
  });

  const clamp = (v: number) => Math.max(0, Math.min(1, v));
  function norm(e: PointerEvent): number[] {
    const r = svgEl.getBoundingClientRect();
    return [clamp((e.clientX - r.left) / r.width), clamp((e.clientY - r.top) / r.height)];
  }

  function onDown(e: PointerEvent) {
    e.preventDefault();
    const p = norm(e);
    status = `↓ ${e.pointerType} ${tool}`;
    if (tool === 'rect') draft = { kind: 'rect', x0: p[0], y0: p[1], x1: p[0], y1: p[1] };
    else if (tool === 'pen') draft = { kind: 'pen', points: [p] };
    else if (tool === 'polygon') { poly = [...poly, p]; return; }
    try { svgEl.setPointerCapture(e.pointerId); } catch { /* iOS may reject */ }
  }
  function onMove(e: PointerEvent) {
    if (!draft) return;
    e.preventDefault();
    const p = norm(e);
    if (draft.kind === 'rect') {
      draft = { ...draft, x1: p[0], y1: p[1] };
      status = `▭ ${Math.round(Math.abs(p[0] - draft.x0) * 100)}×${Math.round(Math.abs(p[1] - draft.y0) * 100)}%`;
    } else if (draft.kind === 'pen') {
      const l = draft.points[draft.points.length - 1];
      if (Math.hypot(p[0] - l[0], p[1] - l[1]) > 0.004) draft = { ...draft, points: [...draft.points, p] };
    }
  }
  async function onUp() {
    if (!draft) return;
    const d = draft; draft = null;
    if (d.kind === 'rect') {
      const x = Math.min(d.x0, d.x1), y = Math.min(d.y0, d.y1);
      const w = Math.abs(d.x1 - d.x0), h = Math.abs(d.y1 - d.y0);
      if (w > 0.005 && h > 0.005) await create('rect', { x, y, w, h });
    } else if (d.kind === 'pen' && d.points.length >= 2) {
      await create('pen_path', { points: d.points, stroke_width: penWidth });
    }
  }
  async function confirmPoly() {
    if (poly.length >= 3) await create('polygon', { points: poly });
    poly = [];
  }

  async function create(shapeType: string, geometry: unknown) {
    status = 'saving…';
    try {
      const a = await createCensorAnnotation(post.id, {
        censorType, shapeType, geometry, negative: eraseMode, clientMutationUuid: crypto.randomUUID(),
      });
      annotations = [...annotations, a];
      status = `${eraseMode ? 'erased' : 'saved'} (${annotations.length})`;
    } catch (e: any) { status = 'save error: ' + e.message; }
  }
  async function remove(a: any) {
    try {
      await deleteCensorAnnotation(post.id, a.id, a.regionVersion);
      annotations = annotations.filter((x) => x.id !== a.id);
      status = 'deleted';
    } catch (e: any) { status = 'delete error: ' + e.message; }
  }
  async function setReview(state: string, hasNoCensor: boolean) {
    try { review = await putCensorReview(post.id, { state, hasNoCensor }); status = 'review: ' + state; }
    catch (e: any) { status = 'review error: ' + e.message; }
  }

  const ptsStr = (pts: number[][]) => pts.map((p) => p.join(',')).join(' ');
  const shapeColor = (a: any) => (a.negative ? ERASE_COLOR : COLORS[a.censorType]);
  const draftColor = () => (eraseMode ? ERASE_COLOR : COLORS[censorType]);
</script>

<Header
  onTogglePool={() => goto('/?mode=pools')}
  onToggleBulk={() => goto('/?bulk=1')}
/>

<div class="wrap">
  <header>
    <a class="back" href={`/post/${post.id}`} aria-label={cm.back(post.id)} title={cm.back(post.id)}>← #{post.id}</a>
    <span class="rev" class:on={review.state === 'complete'}>{review.state}{review.hasNoCensor ? ' · clean' : ''}</span>
    <span class="status">{status}</span>
  </header>

  <div class="toolbar">
    <div class="grp">
      {#each TOOLS as t}
        <button class:active={tool === t} aria-pressed={tool === t} onclick={() => (tool = t)} aria-label={TOOL_NAME[t]} title={TOOL_NAME[t]}>{TOOL_MARK[t]}</button>
      {/each}
      {#if tool === 'polygon'}
        <button class="go" disabled={poly.length < 3} onclick={confirmPoly} aria-label={cm.confirmPolygon(poly.length)} title={cm.confirmPolygon(poly.length)}>✓ {poly.length}</button>
        <button onclick={() => (poly = [])} aria-label={cm.clearPolygon} title={cm.clearPolygon}>✕</button>
      {/if}
      {#if tool === 'pen'}
        {#each PEN_WIDTHS as pw}
          <button class:active={penWidth === pw.v} onclick={() => (penWidth = pw.v)}>{pw.k}</button>
        {/each}
      {/if}
    </div>
    <div class="grp">
      {#each CENSOR_TYPES as c}
        <button class:active={censorType === c} style:--c={COLORS[c]} class="chip" onclick={() => (censorType = c)}>{c}</button>
      {/each}
    </div>
    <div class="grp">
      <button class="erase" class:on={eraseMode} aria-pressed={eraseMode} onclick={() => (eraseMode = !eraseMode)} aria-label={cm.eraserName} title={cm.eraserName}>
        🧹
      </button>
    </div>
    <div class="grp">
      <button onclick={() => setReview('complete', false)} aria-label={cm.markReviewed} title={cm.markReviewed}>✓</button>
      <button onclick={() => setReview('complete', true)} aria-label={cm.noCensor} title={cm.noCensor}>⊘</button>
    </div>
  </div>

  <p class="hint"><b>rect</b>{cm.hintRect}<b>polygon</b>{cm.hintPolygon}<b>pen</b>{cm.hintPen}<b>{cm.hintEraser}</b>{cm.hintEraserBefore}<b>{cm.hintSubtract}</b>{cm.hintEraserAfter}</p>

  <div class="stage">
    <img src={mediaUrl(post.contentUrl)} alt={`post ${post.id}`} draggable="false" onload={measureDispW} />
    <svg
      bind:this={svgEl} viewBox="0 0 1 1" preserveAspectRatio="none"
      onpointerdown={onDown} onpointermove={onMove} onpointerup={onUp} onpointercancel={onUp}
    >
      {#each annotations as a (a.id)}
        {#if a.shapeType === 'rect'}
          <rect x={a.geometry.x} y={a.geometry.y} width={a.geometry.w} height={a.geometry.h}
            fill={shapeColor(a) + (selected === a.id ? '4d' : '1f')} stroke={shapeColor(a)}
            class="shape" class:sel={selected === a.id} class:erase={a.negative} />
        {:else if a.shapeType === 'polygon'}
          <polygon points={ptsStr(a.geometry.points)} fill={shapeColor(a) + (selected === a.id ? '4d' : '1f')}
            stroke={shapeColor(a)} class="shape" class:sel={selected === a.id} class:erase={a.negative} />
        {:else if a.shapeType === 'pen_path'}
          <polyline points={ptsStr(a.geometry.points)} fill="none" stroke={shapeColor(a)}
            stroke-opacity={selected === a.id ? 0.6 : 0.4}
            class="shape penline" class:sel={selected === a.id} class:erase={a.negative}
            style:stroke-width={`${Math.max(2, (a.geometry.stroke_width ?? 0.02) * dispW)}px`} />
        {/if}
      {/each}

      {#if draft?.kind === 'rect'}
        <rect x={Math.min(draft.x0, draft.x1)} y={Math.min(draft.y0, draft.y1)}
          width={Math.abs(draft.x1 - draft.x0)} height={Math.abs(draft.y1 - draft.y0)}
          fill={draftColor() + '1f'} stroke={draftColor()} class="shape draft" />
      {:else if draft?.kind === 'pen'}
        <polyline points={ptsStr(draft.points)} fill="none" stroke={draftColor()} stroke-opacity="0.5"
          class="shape penline draft" style:stroke-width={`${Math.max(2, penWidth * dispW)}px`} />
      {/if}
      {#if poly.length}
        <polyline points={ptsStr(poly)} fill="none" stroke={draftColor()} class="shape draft" />
        {#each poly as p}<circle cx={p[0]} cy={p[1]} r="0.006" fill={draftColor()} />{/each}
      {/if}
    </svg>
  </div>

  <ul class="list">
    {#each annotations as a, i (a.id)}
      <li class:active={selected === a.id}
        onclick={() => (selected = selected === a.id ? null : a.id)}>
        <span class="num" style:background={shapeColor(a)}>{a.negative ? '🧹' : i + 1}</span>
        <span class="meta">{a.negative ? 'erase' : a.censorType} · {a.shapeType}{a.stale ? ' · ⚠ stale' : ''}</span>
        <button class="del" onclick={(e) => { e.stopPropagation(); remove(a); }} aria-label={cm.delete} title={cm.delete}>🗑</button>
      </li>
    {:else}
      <li class="empty">no annotations yet — draw on the image</li>
    {/each}
  </ul>
</div>

<style>
  .wrap { max-width: 1100px; margin: 0 auto; padding: 8px; color: #ddd; overscroll-behavior: contain; }
  header { display: flex; align-items: center; gap: var(--control-gap); padding: 0; }
  .back { color: #74b9ff; text-decoration: none; font-weight: 600; min-height: var(--control-height); box-sizing: border-box; padding: 0 var(--control-padding-x); border-radius: var(--control-radius); font-size: var(--control-font); display: inline-flex; align-items: center; }
  .rev { padding: 2px 8px; border-radius: 10px; background: #444; font-size: 13px; }
  .rev.on { background: #00b894; color: #042; }
  .status { margin-left: auto; opacity: 0.8; font-size: 13px; }
  .toolbar { display: flex; flex-wrap: wrap; gap: var(--control-gap); margin: 4px 0; }
  .hint { margin: 2px 0 8px; font-size: 13px; opacity: 0.75; }
  .hint b { color: #fff; opacity: 1; }
  .grp { display: flex; flex-wrap: wrap; gap: 4px; }
  button { background: #2d3436; color: #ddd; border: 1px solid #555; border-radius: var(--control-radius);
    padding: 0 var(--control-padding-x); font-size: var(--control-font); min-height: var(--control-height); cursor: pointer; box-sizing: border-box; }
  button.active { background: #0984e3; border-color: #0984e3; color: #fff; }
  button.chip.active { background: var(--c); border-color: var(--c); color: #111; }
  button.go { background: #00b894; border-color: #00b894; color: #042; }
  button:disabled { opacity: 0.4; }
  .stage { position: relative; display: inline-block; max-width: 100%; line-height: 0;
    touch-action: none; -webkit-user-select: none; user-select: none; -webkit-touch-callout: none; }
  .stage img { display: block; max-width: 100%; max-height: 78vh; width: auto; height: auto;
    pointer-events: none; -webkit-user-select: none; user-select: none; -webkit-touch-callout: none; }
  .stage svg { position: absolute; inset: 0; width: 100%; height: 100%; z-index: 2;
    touch-action: none; -webkit-touch-callout: none; cursor: crosshair; }
  .shape { stroke-width: 2; vector-effect: non-scaling-stroke; }
  .penline { stroke-linecap: round; stroke-linejoin: round; }
  .shape.draft { stroke-dasharray: 4 3; }
  .shape.erase { stroke-dasharray: 5 3; stroke-width: 3; }
  .shape.sel { stroke-width: 4; filter: drop-shadow(0 0 3px #fff); }
  button.erase.on { background: #ff1744; border-color: #ff1744; color: #fff; }
  .list { list-style: none; padding: 0; margin: 10px 0; }
  .list li { display: flex; align-items: center; gap: var(--control-gap); padding: 0 var(--control-padding-x); border-bottom: 1px solid #333;
    cursor: pointer; border-radius: var(--control-radius); min-height: var(--control-height); }
  .list li.active { background: #2a3340; outline: 1px solid #557; }
  .list .empty { opacity: 0.6; justify-content: center; cursor: default; }
  .num { min-width: 26px; height: 26px; border-radius: 50%; color: #111; font-weight: 700;
    display: inline-flex; align-items: center; justify-content: center; font-size: 14px; }
  .meta { flex: 1; font-size: 14px; }
  .del { margin-left: auto; color: #fff; background: #c0392b; border: none; border-radius: var(--control-radius);
    padding: 0 var(--control-padding-x); min-height: var(--control-height); font-size: var(--control-font); box-sizing: border-box; }
  .del:active { background: #e74c3c; }
</style>
