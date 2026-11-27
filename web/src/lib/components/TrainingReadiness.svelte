<script lang="ts">
  import { onDestroy } from 'svelte';
  import { validateReadiness, readBoundedJSON, type Readiness, type Target } from '../tagger/trainingReadiness';
  let { target, cache, last, active=false, refresh=0 }:{target:Target;cache:Map<string,Promise<Readiness>>;last:Map<string,Readiness>;active?:boolean;refresh?:number}=$props();
  let element=$state<HTMLElement>();
  let visible=$state(false),loading=$state(false),attempted=$state(''),error=$state(false);
  let data=$state<Readiness|null>(null);
  let controller:AbortController|undefined,stopped=false;
  const key=$derived(`${target.queue_sha256}:${target.frame_sha256}:${target.total}:${refresh}`);
  $effect(()=>{
    if(!element||!active){visible=false;return;}
    const observer=new IntersectionObserver(entries=>{visible=entries.some(e=>e.isIntersecting);});
    observer.observe(element);return()=>observer.disconnect();
  });
  $effect(()=>{if(!data&&last.has(target.queue_sha256))data=last.get(target.queue_sha256)??null;});
  $effect(()=>{if(active&&visible&&!loading&&attempted!==key)void load(key);});
  async function request(expected:Target){
    controller=new AbortController();
    const ownController=controller;
    const timeout=setTimeout(()=>ownController.abort(),10000);
    try{
      const response=await fetch(`/api/tagger-dashboard/review-queues/${expected.queue_sha256}/training-readiness`,{cache:'no-store',signal:ownController.signal});
      if(!response.ok)throw new Error('unavailable');
      return validateReadiness(await readBoundedJSON(response),expected);
    }finally{clearTimeout(timeout);}
  }
  async function load(requestKey:string){
    attempted=requestKey;loading=true;error=false;
    const expected={...target};
    try{
      if(!cache.has(requestKey))cache.set(requestKey,request(expected));
      const value=validateReadiness(await cache.get(requestKey),expected);
      if(!stopped&&requestKey===key){data=value;last.set(expected.queue_sha256,value);}
    }catch{if(!stopped&&requestKey===key)error=true;}
    finally{if(!stopped)loading=false;}
  }
  onDestroy(()=>{stopped=true;controller?.abort();});
</script>
<section class="readiness" aria-label="次の学習入力の照合" bind:this={element}>
  <h3>次の学習入力の照合</h3>
  {#if error}<p role="alert">照合結果を取得できません。対象に使えるかは不明です。{data?'以下は前回の照合結果です。':''}</p>{/if}
  {#if loading}<p>回答と現在の学習ラベルを読み取っています…</p>{/if}
  {#if data}
    {#if data.counts.unseen}<p>未回答の画像は上のリンクで確認できます。</p>
    {:else if data.counts.uncertain}<p><strong>回答完了・判断不能あり</strong>。無理な再回答は不要です。</p>
    {:else if data.state==='responses_bound_not_qualified'}<p><strong>回答と学習ラベルの照合完了</strong>。学習開始の判定は別です。</p>
    {:else if data.counts.gt_not_synced_to_response}<p><strong>回答は保存済み・学習用ラベルとの同期待ち</strong>です。同じ回答をやり直す必要はありません。</p>
    {:else if data.counts.superseded_response}<p>別の確認で新しい回答が記録されています。今回の回答数と現在の対象外件数は別に表示します。</p>
    {:else}<p>回答時の固定と現在の固定が一致しません。過去の回答で現在の固定を上書きせず、次の入力から対象画像を除外します。</p>{/if}
    <table>
      <caption>この確認セットの{data.total}枚（タグ全体の件数ではありません）</caption>
      <tbody>
        <tr><th>今回の回答とラベル一致</th><td>有 {data.current_definite.positive} / 無 {data.current_definite.negative}枚</td></tr>
        {#if data.counts.unseen}<tr><th>未回答</th><td>{data.counts.unseen}枚</td></tr>{/if}
        {#if data.counts.uncertain}<tr><th>判断不能の回答</th><td>{data.counts.uncertain}枚</td></tr>{/if}
        {#if data.counts.gt_not_synced_to_response}<tr><th>学習ラベルの同期待ち</th><td>{data.counts.gt_not_synced_to_response}枚</td></tr>{/if}
        {#if data.counts.current_pin_not_bound_to_response}<tr><th>現在の固定と対応不一致</th><td>{data.counts.current_pin_not_bound_to_response}枚</td></tr>{/if}
        {#if data.counts.superseded_response}<tr><th>別の確認で更新済み</th><td>{data.counts.superseded_response}枚</td></tr>{/if}
        {#if data.excluded_from_next_training>0 || data.counts.uncertain}<tr><th>現在の学習対象外</th><td>{data.excluded_from_next_training}枚</td></tr>{/if}
        <tr><th>独立グループ数</th><td>未計測</td></tr>
      </tbody>
    </table>
    {#if data.counts.uncertain && data.excluded_from_next_training===0}<p>判断不能の回答は残っていますが、別の確認で更新された回答により、現在の照合では対象外は0枚です。</p>{/if}
    <small>照合日時 {new Date(data.observed_at).toLocaleString('ja-JP')}</small>
    <details><summary>照合の範囲・未計測の項目</summary>
      <p>独立グループ数はまだシステムで計測していません。この未計測表示のための再回答は不要です。</p>
      <p>このセットの対象タグについて、次に作る学習入力への利用を照合しています。過去の学習ラベル・固定・モデルは変更しません。この表示から学習・公開は始まりません。</p>
    </details>
  {:else if !loading&&!error}<p class="muted">表示時に照合します。学習は開始しません。</p>{/if}
</section>
<style>
  .readiness{border-top:1px solid var(--line);margin-top:16px;padding-top:12px;font-size:.92rem;line-height:1.65;overflow-wrap:anywhere}
  table{width:100%;border-collapse:collapse;font-size:.86rem}caption{text-align:left;color:var(--text-dim);font-size:.8rem;margin-bottom:4px}th,td{padding:4px 0;border-bottom:1px solid var(--line);text-align:left;font-weight:normal}td{text-align:right;white-space:nowrap;padding-left:8px}details{margin-top:8px}summary{cursor:pointer}
  h3{font-size:1rem;margin:0 0 8px}p{margin:8px 0}.muted,small{color:var(--text-dim)}[role=alert]{color:var(--warning,var(--text))}
</style>
