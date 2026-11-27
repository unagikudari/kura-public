<script module lang="ts">
  export type Comparison = {state:'unavailable'} | {
    state:'available';answered:number;total:number;complete:boolean;
    tags:{tag:string;answered:number;total:number}[];
    verdict:null|{admissible:boolean;reasons:string[];tags:Record<string,{f1_change:number|null;regression:boolean}>};
  };
</script>

<script lang="ts">
  let {data}:{data:Comparison} = $props();
  const reasonLabels:Record<string,string> = {
    priority_tags_unanswered:'未回答の優先タグがある',
    priority_tag_undecidable:'比較を確定できない優先タグがある',
    priority_tag_regression:'優先タグに有意な退行がある',
    priority_tag_below_precision_floor:'優先タグの適合率が基準未達',
    no_overall_improvement:'全体のF1が改善していない',
  };
  function delta(value:number|null|undefined) {
    return value == null ? '未算出' : `${value > 0 ? '+' : ''}${(value * 100).toFixed(2)} pt`;
  }
</script>

<section aria-label="EVA02 比較審査">
  <h3>EVA02 比較審査</h3>
  {#if data.state === 'unavailable'}
    <p>比較審査の登録を確認できません。</p>
  {:else}
    <p><strong>{data.answered}/{data.total}件</strong> · {data.tags.length}タグ</p>
    {#if data.verdict}
      <p><strong>{data.verdict.admissible ? '採用基準を満たす' : '採用不可'}</strong></p>
      {#if data.verdict.reasons.length}
        <ul>{#each data.verdict.reasons as reason}<li>{reasonLabels[reason] ?? reason}</li>{/each}</ul>
      {/if}
    {:else if data.complete}
      <p>全回答を確認しました。現行の回答に対応する判定結果を待っています。</p>
    {:else}
      <p>回答が揃うと判定結果を表示します。<a href="/tagger-dashboard#review">審査を開く</a></p>
    {/if}
    <details open={data.complete}>
      <summary>タグ別の回答数{data.verdict ? '・判定' : ''}</summary>
      <table>
        <thead><tr><th>タグ</th><th>回答</th>{#if data.verdict}<th>F1差</th><th>有意な退行</th>{/if}</tr></thead>
        <tbody>
          {#each data.tags as row (row.tag)}
            <tr>
              <th scope="row">{row.tag}</th><td>{row.answered}/{row.total}</td>
              {#if data.verdict}
                <td>{delta(data.verdict.tags[row.tag]?.f1_change)}</td>
                <td>{data.verdict.tags[row.tag]?.regression ? 'あり' : '検出なし'}</td>
              {/if}
            </tr>
          {/each}
        </tbody>
      </table>
    </details>
  {/if}
</section>

<style>
  section{margin-top:12px}h3{font-size:1rem;margin:0 0 4px}p{margin:4px 0}
  table{border-collapse:collapse;width:100%;font-size:.9rem}th,td{text-align:left;padding:4px;border-bottom:1px solid var(--line);overflow-wrap:anywhere}
</style>
