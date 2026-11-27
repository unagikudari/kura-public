<script lang="ts">
  import { validComparison, disagreementLink, type Comparison } from '../tagger/reviewComparison';
  let { tag, total, label, comparison, compact = false }: {tag:string; total:number; label:string; comparison:Comparison; compact?:boolean} = $props();
  const valid = $derived(validComparison(comparison,total));
  const parts = $derived(valid ? [
    {label:'ありで一致',value:comparison.counts!.tp,color:'#278265'},
    {label:'なしで一致',value:comparison.counts!.tn,color:'#648e80'},
    {label:'モデルだけが「あり」',value:comparison.counts!.fp,color:'#c85c29'},
    {label:'人だけが「あり」',value:comparison.counts!.fn,color:'#8b65bc'},
    {label:'未比較',value:comparison.remaining!,color:'#8a8a8a'}
  ] : []);
  const disagreements = $derived(valid ? comparison.counts!.fp + comparison.counts!.fn : 0);
  const safeLink = $derived(disagreementLink(comparison));
</script>
<article class="comparison" aria-label={`${tag}のモデル比較`}>
  {#if !compact}<h3>{tag} · {label}</h3>{/if}
  {#if valid}
    {#if comparison.compared === 0}<p>まだ比較できる判定がありません。</p>{:else}
    {#if !compact}<p><strong>{comparison.compared}/{total}枚を比較 · 不一致{disagreements}枚</strong></p>{/if}
    <div class="bar" aria-hidden="true">{#each parts as p}<span style={`flex:${p.value};background:${p.color}`}></span>{/each}</div>
    <ul>{#each parts as p}<li><span style={`background:${p.color}`} aria-hidden="true"></span>{p.label}<strong>{p.value}枚</strong></li>{/each}</ul>
    {#if !compact && disagreements && safeLink}<a href={safeLink}>不一致の{disagreements}枚を見る（任意）</a>{/if}
    {/if}
    <details><summary>採点情報を開く</summary><p>モデル {comparison.model_sha256?.slice(0,8)} · 閾値 {comparison.threshold}<br>固定スコア {comparison.score_sha256?.slice(0,8)}。本番の採用状態はこの比較データからは確認できません。</p></details>

  {:else}<p role="alert">比較結果を確認できません。一致・改善済みとは判断できません。</p>{/if}
</article>
<style>
.comparison{padding:0;min-width:0}h3{font-size:15px;margin:0 0 10px}p{line-height:1.6;margin:8px 0}.note{font-size:13px;opacity:.8}.bar{display:flex;height:16px;border-radius:4px;overflow:hidden;background:var(--line)}.bar span{min-width:0}ul{list-style:none;padding:0;margin:12px 0;display:flex;flex-wrap:wrap;gap:6px 18px}li{display:flex;align-items:center;gap:8px;font-size:13px}li span{width:10px;height:10px;flex-shrink:0}li strong{margin-left:3px}a{color:var(--accent);display:inline-flex;padding:8px 0;min-height:44px;align-items:center}summary{cursor:pointer;min-height:44px;padding:12px 0;font-size:13px}
</style>
