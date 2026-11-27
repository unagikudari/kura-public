<script lang="ts">
  import { onMount } from 'svelte';
  import BaseComparisonStatus from './BaseComparisonStatus.svelte';
  import BaseVocabularyStatus from './BaseVocabularyStatus.svelte';
  import type { Vocabulary } from './BaseVocabularyStatus.svelte';
  import type { Comparison } from './BaseComparisonStatus.svelte';
  let {active=false}:{active?:boolean}=$props();
  type Experiment={tag:string;host:string;phase:string;done:number;total:number;observed_at:number;age_seconds:number;fresh:boolean;published:false};
  type Learning={tag:string;label:string;state:string;fresh:boolean;observed_at:number};
  const webLabels = {current:'現行',pending:'配備待ち',mismatch:'配信不一致',unobserved:'観測不能'};
  type WebRelease = {state:keyof typeof webLabels;observed_at?:number};
  type HumanSupport = {tag:string;observed:number;required:number;needed:number;edit_url:string};
  type Snapshot={state:string;selected_release:number|null;machine_output_holds:string[];observed_at:string;learning:Learning|null;experiment:Experiment|null;web_release?:WebRelease;human_support?:HumanSupport[];base_comparison?:Comparison;base_vocabulary?:Vocabulary};
  let data=$state<Snapshot|null>(null), error=$state(''), busy=$state(false), stopped=false, clock=$state(0), receivedAt=$state(0);
  const labels:Record<string,string>={extracting:'特徴抽出',training:'学習',evaluating:'機械による候補の採点',qualifying:'メモリ・速度を確認中（品質評価とは別）',completed:'この実験は完了・品質は未承認',stopped:'停止・原因確認が必要'};
  const experimentLive=$derived(data?.experiment && data.experiment.fresh && data.experiment.age_seconds+(clock-receivedAt)/1000<=120);
  const snapshotOld=$derived(data && clock-receivedAt>120000);
  const experimentStatus=$derived(
    data?.experiment && (data.experiment.phase==='completed' || (experimentLive && !snapshotOld && !error))
      ? labels[data.experiment.phase]
      : '現在の稼働は未確認'
  );
  const webRelease = $derived(data?.web_release && Object.hasOwn(webLabels,data.web_release.state) && !snapshotOld && !error
    ? data.web_release : null);
  let inFlight: Promise<void> | null = null;
  export function refresh(): Promise<void> {
    if (inFlight) return inFlight;
    inFlight = loadSnapshot().finally(() => { inFlight = null; });
    return inFlight;
  }
  async function loadSnapshot() {
    if(busy)return;busy=true;
    try{
      const r=await fetch('/api/tagger-dashboard/summary',{cache:'no-store',signal:AbortSignal.timeout(10000)});
      if(!r.ok)throw new Error('unavailable');const d=await r.json();
      if(!(d.selected_release===null || Number.isInteger(d.selected_release)&&d.selected_release>0)||d.state!=='available'||!Array.isArray(d.machine_output_holds)||d.machine_output_holds.some((t:unknown)=>typeof t!=='string')||typeof d.observed_at!=='string'||!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(d.observed_at)||!Number.isFinite(Date.parse(d.observed_at)))throw new Error('invalid');
      if(d.experiment && (typeof d.experiment.tag!=='string'||typeof d.experiment.host!=='string'||!Object.hasOwn(labels,d.experiment.phase)||d.experiment.published!==false||![d.experiment.done,d.experiment.total].every(n=>Number.isInteger(n)&&n>=0)||d.experiment.done>d.experiment.total||!Number.isFinite(d.experiment.observed_at)||!Number.isFinite(d.experiment.age_seconds)||d.experiment.age_seconds<0||typeof d.experiment.fresh!=='boolean'))throw new Error('experiment_invalid');
      if(d.learning && (typeof d.learning.tag!=='string'||typeof d.learning.label!=='string'||typeof d.learning.fresh!=='boolean'||!Number.isFinite(d.learning.observed_at)))throw new Error('learning_invalid');
      if(!stopped){data=d;error='';clock=performance.now();receivedAt=clock;}
    }catch{if(!stopped)error='現在の状況を取得できません。';}finally{busy=false;}
  }
  $effect(()=>{if(active&&!data&&!busy&&!error)void refresh();});
  onMount(()=>{const tick=()=>{clock=performance.now();if(active&&!document.hidden)void refresh();};const t=setInterval(tick,30000);document.addEventListener('visibilitychange',tick);return()=>{stopped=true;clearInterval(t);document.removeEventListener('visibilitychange',tick);};});
</script>
<section hidden={!active} class="runtime" aria-label="本番と改善処理の現在地">
  <header><h2>本番と改善処理</h2>{#if data}<small class="summary-observed">集計時刻 <time datetime={data.observed_at}>{new Date(data.observed_at).toLocaleString('ja-JP')}</time></small>{/if}</header>
  {#if error || snapshotOld}<p role="alert">{error || '状況の観測が古くなっています。'}{data?' 以下は最後に取得した記録です。':''}</p>{/if}
  {#if data}
    <div class="line"><span>本番 {data.selected_release === null ? '未選択' : `release ${data.selected_release}`}</span><span>自動付与停止 {data.machine_output_holds.length}タグ</span></div>
    <p class="current-experiment">
      {#if data.experiment}{data.experiment.tag} · 未公開 · {experimentStatus}
      {:else}改善実験 · {experimentStatus}{/if}
    </p>
    {#if data.learning}
      <p class="current-learning">{data.learning.tag} · {data.learning.fresh && !snapshotOld && !error ? data.learning.label : '現在の稼働は未確認'}</p>
    {/if}
    <details class="runtime-details"><summary>学習・稼働の詳細</summary>
    <div class="line"><strong>本番の選択</strong><span>{data.selected_release===null?'未選択':`release ${data.selected_release}`}</span><a href="/tagger-dashboard#nodes">ノードへの反映を確認</a></div>
    <div class="line"><strong>機械による付与停止</strong><span>{data.machine_output_holds.length}タグ{#if data.machine_output_holds.length>0 && data.machine_output_holds.length<=5}: {data.machine_output_holds.join(' / ')}{/if}</span>{#if data.machine_output_holds.length>0}<details><summary>{data.machine_output_holds.length>5?'対象と理由':'停止の理由'}</summary>{#if data.machine_output_holds.length>5}<p>{data.machine_output_holds.join(' / ')}</p>{/if}<p>誤付与を防ぐためAPIで受付を停止。人の編集は可能です。既存タグは削除しません。解除には修正版の品質確認が必要です。</p></details>{/if}</div>
    {#if data.experiment}
      {@const x=data.experiment}
      <div class="line experiment"><strong>{x.tag} の{x.phase==='completed'?'保存済み実験結果':'実験'}</strong><span>未公開 · {x.phase==='completed' || (experimentLive && !snapshotOld && !error) ? labels[x.phase] : `最後の報告: ${labels[x.phase]}（過去）・現在の稼働は未確認`}</span><span>{x.phase==='completed'?'当時の実行環境（申告）':'報告された実行環境'}: {x.host}</span>{#if x.phase==='completed'}<small>実行・資源の観測記録は品質承認ではありません。</small>{/if}
      {#if experimentLive && !snapshotOld && !error && ['extracting','training','evaluating','qualifying'].includes(x.phase) && x.total>0}<progress value={x.done} max={x.total} aria-label={`${x.tag} · ${labels[x.phase]} ${x.done}/${x.total}`}></progress><span>{x.done}/{x.total} {x.phase==='training'?'epoch':x.phase==='extracting'?'枚':'工程'}</span>{/if}
      <small>{x.phase==='completed'?'記録日時':'観測'} {new Date(x.observed_at*1000).toLocaleString('ja-JP')}</small></div>
    {:else}<p>改善実験の報告を確認できません。稼働の有無は未確認です。</p>{/if}
    {#if data.learning}<details class="learning-history"><summary>自動学習の記録（{data.learning.fresh && !snapshotOld && !error ? '直近' : '過去'}）</summary><div class="line"><span>{data.learning.tag} · {data.learning.fresh && !snapshotOld && !error ? data.learning.label : `最後の報告: ${data.learning.label}（過去）・現在の稼働は未確認`}</span><small>観測 {new Date(data.learning.observed_at*1000).toLocaleString('ja-JP')}</small></div></details>{/if}

    </details>
  {:else if !error}<p role="status">現在地を取得中…</p>{/if}
    <p class="line web-release-status">Web 配備 <strong>{webLabels[webRelease?.state ?? 'unobserved']}</strong>
      {#if webRelease?.observed_at}<time datetime={new Date(webRelease.observed_at * 1000).toISOString()}>{new Date(webRelease.observed_at * 1000).toLocaleString('ja-JP')}</time>{/if}
    </p>
  {#if data?.base_comparison && !snapshotOld && !error}<BaseComparisonStatus data={data.base_comparison} />{/if}
  {#if data?.human_support?.length && !snapshotOld && !error}
    <div class="human-support">
      <h3>学習に必要な人の確認</h3>
      <ul>
        {#each data.human_support as item (item.tag)}
          <li>
            <strong>{item.tag}</strong>
            {#if item.needed}
              <span>人が確かめた正例はあと{item.needed}件（{item.observed}/{item.required}件）</span>
              <a href={item.edit_url}>一括編集で確認・施錠</a>
            {:else}
              <span>正例は必要数に到達（{item.observed}/{item.required}件）。次の判定待ち</span>
            {/if}
          </li>
        {/each}
      </ul>
    </div>
  {/if}
  {#if data?.base_vocabulary && !snapshotOld && !error}<BaseVocabularyStatus data={data.base_vocabulary} />{/if}
</section>
<style>
.human-support h3{font-size:1rem;margin:8px 0 4px}.human-support ul{list-style:none;margin:0;padding:0}.human-support li{display:flex;flex-wrap:wrap;gap:4px 12px;margin:4px 0;overflow-wrap:anywhere}
.current-experiment,.current-learning{margin:4px 0;overflow-wrap:anywhere}.runtime-details>summary{min-height:32px}

.runtime{padding:12px 24px;border-bottom:1px solid var(--line);flex-shrink:0}header,.line{display:flex;align-items:center;gap:8px 14px;flex-wrap:wrap}header{justify-content:space-between}h2{font-size:16px;margin:0}.line{overflow-wrap:anywhere;min-width:0;font-size:14px;margin:4px 0}.line strong{min-width:118px}.note,small{font-size:12px}p{line-height:1.5}summary,a{min-height:44px;display:inline-flex;align-items:center}a{color:var(--accent)}summary{cursor:pointer}summary::before{content:"▸";margin-right:6px}details[open]>summary::before{content:"▾"}progress{width:100px;height:12px}details p{max-width:600px}@media(max-width:700px){.runtime{padding:12px 16px}.line strong{min-width:108px}}
</style>
