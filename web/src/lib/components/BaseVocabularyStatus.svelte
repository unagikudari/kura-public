<script module lang="ts">
  export type Vocabulary = {state:'unavailable'} | {
    state:'available';tags:{name:string;category:number}[];
  };
</script>

<script lang="ts">
  let {data}:{data:Vocabulary} = $props();
  const labels:Record<number,string>={0:'一般',1:'作者',3:'作品',4:'キャラクター',5:'メタ'};
  const batch = 30;
  let query = $state(''), visible = $state(batch);
  const matches = $derived(data.state === 'available'
    ? data.tags.filter(tag => tag.name.toLowerCase().includes(query.trim().toLowerCase())) : []);
</script>

<section aria-label="EVA02 の保留語彙">
  <h3>EVA02 の保留語彙</h3>
  {#if data.state === 'unavailable'}
    <p>語彙レポートを取得できません。</p>
  {:else if !data.tags.length}
    <p>保留中の語彙はありません。</p>
  {:else}
    <details>
      <summary>未登録の語彙 {data.tags.length}件</summary>
      <label>語彙を検索 <input type="search" bind:value={query} oninput={() => { visible = batch; }} /></label>
      <ul>
        {#each matches.slice(0, visible) as tag (tag.name)}
          <li>{tag.name} <small>{labels[tag.category] ?? `カテゴリ ${tag.category}`}</small></li>
        {/each}
      </ul>
      {#if !matches.length}<p>一致する語彙はありません。</p>{/if}
      {#if matches.length > visible}<button onclick={() => { visible += batch; }}>さらに表示</button>{/if}
    </details>
  {/if}
</section>

<style>
  section{margin-top:12px}h3{font-size:1rem;margin:0 0 4px}p{margin:4px 0}
  label{display:block;margin:8px 0}input{max-width:100%}ul{padding-left:1.4em}
  li{overflow-wrap:anywhere}small{opacity:.75}
</style>
