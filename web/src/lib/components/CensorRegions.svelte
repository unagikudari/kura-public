<script lang="ts">
  // Read-only overlay of a post's censor_annotation regions, for confirming
  // edit locations in the manga viewer + lightbox (not for editing — that's
  // /post/<id>/censor). Translucent fill so the underlying art stays readable.
  // viewBox=image dims + preserveAspectRatio=meet lines the regions up with the
  // image whether it is object-fit:contain (lightbox) or fills its box (viewer).
  import { listCensorAnnotations } from '#lib/api.ts';

  // fit: 画像の object-position に合わせる (lightbox ペア表示は左セル右寄せ →
  // xMaxYMid)。既定は中央 (単独表示 object-fit:contain 中央と一致)。
  let { postId, w, h, show = false, enabled = true, fit = 'xMidYMid' }: {
    postId: number; w?: number | null; h?: number | null; show?: boolean; enabled?: boolean; fit?: string;
  } = $props();

  const COLORS: Record<string, string> = {
    mosaic: '#e84393', white_bar: '#0984e3', black_bar: '#00b894',
    slash: '#fdcb6e', other: '#b2bec3',
  };
  const ERASE = '#ff1744';   // eraser/negative regions = subtractive (red, dashed)

  let anns = $state<any[]>([]);
  let loadedFor = $state<number | null>(null);
  // The post this component last *asked* about — answers "do we still need to
  // ask for THIS post?", which a boolean "loading" cannot: while a request for
  // post A is in flight the lightbox may already have moved to post B.
  let requestedFor: number | null = null;
  // ...but a post id does not answer "is this response still the current one?"
  // either, because ids come back: A -> B -> A issues a SECOND request for A
  // while the first is still in flight, and both of them match requestedFor.
  // Only a value that is never reused can say which request owns the view, so
  // every issued request takes the next generation number and the newest one
  // wins. Late arrivals — success OR failure — are discarded by generation.
  let generation = 0;

  // Fetch lazily: only when shown, enabled, and not already loaded/asked for
  // THIS post. postId is captured by value up front, so every later decision
  // is made against the id the request was issued for rather than whatever
  // post happens to be on screen when the promise settles.
  $effect(() => {
    const id = postId;
    if (!show || !enabled || loadedFor === id || requestedFor === id) return;
    const gen = ++generation;
    requestedFor = id;
    listCensorAnnotations(id)
      .then((r: any[]) => {
        if (gen !== generation) return;   // superseded: a newer request owns the view
        anns = r;
        loadedFor = id;
      })
      // Release the slot only if this request is still the owner. A late
      // failure from a superseded request must not clear the in-flight
      // bookkeeping of the request that replaced it.
      .catch(() => { if (gen === generation) requestedFor = null; });
  });

  const col = (a: any) => (a.negative ? ERASE : (COLORS[a.censorType] ?? COLORS.other));
  const scaled = (pts: number[][]) =>
    pts.map((p) => `${p[0] * (w as number)},${p[1] * (h as number)}`).join(' ');
</script>

{#if show && enabled && w && h && loadedFor === postId && anns.length}
  <svg class="cr" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio={`${fit} meet`} aria-hidden="true">
    {#each anns as a (a.id)}
      {#if a.shapeType === 'rect'}
        <rect x={a.geometry.x * w} y={a.geometry.y * h} width={a.geometry.w * w} height={a.geometry.h * h}
          fill={a.negative ? 'none' : col(a)} fill-opacity={a.negative ? 0 : 0.22}
          stroke={col(a)} class="cr-s" class:er={a.negative} />
      {:else if a.shapeType === 'polygon'}
        <polygon points={scaled(a.geometry.points)}
          fill={a.negative ? 'none' : col(a)} fill-opacity={a.negative ? 0 : 0.22}
          stroke={col(a)} class="cr-s" class:er={a.negative} />
      {:else if a.shapeType === 'pen_path'}
        <polyline points={scaled(a.geometry.points)} fill="none" stroke={col(a)} stroke-opacity="0.4"
          stroke-linecap="round" stroke-linejoin="round"
          stroke-width={Math.max(1, (a.geometry.stroke_width ?? 0.02) * w)} class:er={a.negative} />
      {/if}
    {/each}
  </svg>
{/if}

<style>
  .cr { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; z-index: 3; }
  .cr-s { stroke-width: 2; vector-effect: non-scaling-stroke; }
  .cr-s.er { stroke-dasharray: 5 3; }
</style>
