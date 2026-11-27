<script lang="ts">
 import Header from '#lib/components/Header.svelte';
 import { takeReviewPrefetch } from '#lib/tagger/reviewPrefetch.ts';
 import { thumbnailLoader } from '#lib/thumbnailLoader.ts';
 import { goto } from '$app/navigation';
 import { page } from '$app/state';
 import { onDestroy, untrack } from 'svelte';
 import { MUT, fetchPost, fetchPosts, mediaUrl, fetchPostPinStates, fetchTagDetail, patchPost, putTagPin, deletePost } from '../../../lib/api';
 import { patchTagVersionAware } from '#lib/bulkTag.ts';
 import ThumbCategoryTags from '#lib/components/ThumbCategoryTags.svelte';
 import type { Post } from '#lib/types.ts';

 import { reviewRequestPath, reviewMediaPath } from '../../../lib/tagger/humanReviewTransport';
 let set:any=$state(null), image:any=$state(null), at=$state(0), busy=$state(false), error=$state(''), pending:any=$state(null), imageUrl=$state(''), imageReady=$state(false);
 // Direct (/3) requests never apply tags; a base comparison must not show the incumbent's tags.
 const evaluationOnly = $derived(['dedicated_candidate', 'base_comparison'].includes(set?.source_provenance?.kind) || set?.adjudication === true);
 let groupSelected = $state<string[]>([]);
 const groupKeys = "asdfghjk";
 let serial=0, activeRef='';
 function chooseOption(tag:string){
  if(!image?.group||busy||editingPin||!imageReady)return;
  if(groupSelected.includes(tag)){groupSelected=groupSelected.filter(value=>value!==tag);return;}
  if(image.group.mode==='single'){groupSelected=[tag];return;}
  const conflicts=new Set<string>(image.group.exclusive.filter((pair:string[])=>pair.includes(tag)).flat());
  groupSelected=[...groupSelected.filter(value=>!conflicts.has(value)),tag];
 }
 let imageRequest: AbortController | null = null;
 let loadingImage=$state(false);
 let category = $state('');
 let tilePosts = $state<Record<number, Post>>({});
 let tilePins = $state<Record<number, Record<string, 'present' | 'absent'>>>({});
 let editingPin = $state(false);
 function boundedPatchDeps(signal: AbortSignal) {
  return {
   fetchPost: (id: number) => fetchPost(id, (input, init) => fetch(input, {...init, signal})),
   patchPost: (id: number, version: number, tags: string[]) => patchPost(id, version, tags, undefined, signal),
  };
 }
 const patchDeps = {
  fetchPost: (id: number) => boundedPatchDeps(AbortSignal.timeout(12000)).fetchPost(id),
  patchPost: (id: number, version: number, tags: string[]) =>
   boundedPatchDeps(AbortSignal.timeout(12000)).patchPost(id, version, tags),
 };
 $effect(() => {
  const tag = set?.tag;
  category = '';
  if (!tag || evaluationOnly) return;
  let active = true;
  void fetchTagDetail(tag).then(detail => { if (active) category = detail.category; }, () => {});
  return () => { active = false; };
 });
 $effect(() => {
  if (!set || evaluationOnly) return;
  const ids: number[] = set.post_ids.slice(Math.max(0, at - 3), at + 4);
  if (!ids.length) return;
  const controller = new AbortController();
  const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]);
  void fetchPosts('ids:' + ids.join(','), 0, ids.length, fetch, signal).then(result => {
   if (!signal.aborted) for (const post of result.results) tilePosts[post.id] = post;
  }, () => {});
  void fetchPostPinStates(ids, signal).then(pins => {
   if (!signal.aborted) for (const id of ids) tilePins[id] = pins[id] ?? {};
  }, () => {});
  return () => controller.abort();
 });
 async function refreshTile(pid: number, updated?: Post) {
  const token = serial, ref = activeRef;
  if (updated) tilePosts[pid] = updated;
  const current = set?.post_ids[at] === pid && image;
  const wasReady = imageReady;
  if (current) imageReady = false;
  const [pins, observed] = await Promise.all([
   fetchPostPinStates([pid], AbortSignal.timeout(8000)),
   current ? read(ref, '/posts/' + pid) : Promise.resolve(null),
  ]);
  if (token !== serial) return;
  tilePins[pid] = pins[pid] ?? {};
  if (observed && image) {
   if (observed.context.display_sha256 !== image.context.display_sha256) {
    await show(at);
   } else {
    image = observed;
    pending = null;
    imageReady = wasReady || imageReady;
   }
  }
 }
 async function toggleTilePin(pid: number, tag: string) {
  if (busy || editingPin) return;
  editingPin = true;
  try {
   await putTagPin(pid, tag, tilePins[pid]?.[tag] === 'present' ? null : 'present', AbortSignal.timeout(12000));
   await refreshTile(pid);
  } catch {
   error = '鍵の状態を更新できませんでした。再読み込みしてください。';
  } finally {
   editingPin = false;
  }
 }
 function tileUpdated(post: Post) {
  void refreshTile(post.id, post).catch(() => {
   error = 'タグの状態を取得できませんでした。再読み込みしてください。';
  });
 }
 let referencePosts = $state<Post[]>([]);
 let enlargedReference = $state<Post | null>(null);
 let referenceDialog: HTMLDialogElement;
 function enlargeReference(post: Post) {
  enlargedReference = post;
  referenceDialog.showModal();
 }
 const referenceIds = $derived((set?.post_ids ?? []).join(','));
 $effect(() => {
  const tag = set?.tag;
  const ids = referenceIds;
  referencePosts = [];
  if (!tag) return;
  const controller = new AbortController();
  const reviewed = new Set(ids.split(',').map(Number));
  void (async () => {
   const pinned = await fetchPosts('locked:' + tag, 0, 24, fetch, controller.signal).catch(() => null);
   if (controller.signal.aborted) return;
   const examples = (pinned?.results ?? []).filter(post => post.type === 'image' && !reviewed.has(post.id)).slice(0, 4);
   if (examples.length < 4) {
    const tagged = await fetchPosts(tag, 0, 24, fetch, controller.signal).catch(() => null);
    const shown = new Set(examples.map(post => post.id));
    examples.push(...(tagged?.results ?? []).filter(post => post.type === 'image' && !reviewed.has(post.id) && !shown.has(post.id))
      .slice(0, 4 - examples.length));
   }
   if (!controller.signal.aborted) referencePosts = examples;
  })().catch(() => {});
  return () => controller.abort();
 });
 const canDiscard = $derived(!evaluationOnly && ['historical_label_recheck', 'training_support'].includes(set?.purpose));
 async function discard(remove: boolean) {
  if (busy || editingPin || !canDiscard || !imageReady) return;
  const pid = set.post_ids[at];
  if (remove && !window.confirm('画像 #' + pid + ' を削除します。この操作は取り消せません。')) return;
  const token = serial, ref = activeRef, index = at;
  busy = true;
  error = '';
  try {
   const signal = AbortSignal.timeout(12000);
   if (remove) {
    await deletePost(pid, signal);
   } else {
    const deps = boundedPatchDeps(signal);
    const post = await deps.fetchPost(pid);
    await patchTagVersionAware(post, tags => [...new Set([...tags, 'tagger-training-exclude'])], deps);
   }
   if (token !== serial) return;
   imageReady = false;
   const value = await read(ref, '');
   if (token !== serial) return;
   set = value;
   if (!value.remaining) {
    await nextSet(ref);
    return;
   }
   const next = value.post_ids.findIndex((id: number, i: number) => i >= index && !value.decisions[id]);
   await show(next < 0 ? value.post_ids.findIndex((id: number) => !value.decisions[id]) : next);
  } catch (e) {
   if (token === serial) error = reviewError(e);
  } finally {
   if (token === serial) busy = false;
  }
 }
 const thumb=(n:number)=>`${MUT}/post/${set.post_ids[n]}/thumbnail`;
 const names:Record<string,string>={present:'有る',absent:'無い',uncertain:'判別できない'};
 function clearImage(){
  imageRequest?.abort();imageRequest=null;loadingImage=false;
  if(imageUrl)URL.revokeObjectURL(imageUrl);
  imageUrl='';image=null;imageReady=false;groupSelected=[];
 }
 async function read(ref:string,path:string,signal=AbortSignal.timeout(12000)){const r=await fetch(reviewRequestPath(MUT,ref,path),{cache:'no-store',signal});if(!r.ok)throw new Error('読み込みに失敗しました。ページを再読み込みしてください。');return r.json();}
 async function imageAt(ref:string,n:number,token:number,signal:AbortSignal){
  const pid=set.post_ids[n];const value=await read(ref,'/posts/'+pid,signal);
  if(token!==serial)return;
  if(value.content_url!==`/api/post/${pid}/content`)throw new Error('画像参照が一致しません。');
  const response=await fetch(reviewMediaPath(MUT,value.content_url,pid),{cache:'no-store',signal});if(!response.ok||!response.body)throw new Error('画像を取得できませんでした。');
  const max=16*1024*1024;const reader=response.body.getReader();const chunks:Uint8Array[]=[];let size=0;
  for(;;){const {done,value:chunk}=await reader.read();if(done)break;size+=chunk.length;if(size>max){await reader.cancel();throw new Error('画像が16MiBを超えました。ここでは判定せず別画面で確認してください。');}chunks.push(chunk);}
  const bytes=new Uint8Array(size);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))).map(x=>x.toString(16).padStart(2,'0')).join('');
  if(hash!==value.context.display_sha256)throw new Error('表示画像が確認対象と異なります。判定せず再読み込みしてください。');
  signal.throwIfAborted();
  if(token!==serial)return;
  image=value;imageUrl=URL.createObjectURL(new Blob([bytes],{type:response.headers.get('content-type')||'image/webp'}));
 }
 function displayResult(event: Event, ready: boolean) {
  const shown = event.currentTarget as HTMLImageElement;
  if (shown.getAttribute('src') !== imageUrl) return;
  imageReady=ready;
  if (!ready) error='画像を表示できません。判定せず再読み込みしてください。';
 }
 function reviewError(e: unknown) {
  return e instanceof DOMException && (e.name === 'TimeoutError' || e.name === 'AbortError')
   ? '通信に時間がかかっています。画像を再読み込みするか、保存中なら同じ回答を再送してください。'
   : e instanceof Error ? e.message : String(e);
 }
 async function load(ref=activeRef){const token=++serial;busy=true;error='';clearImage();pending=null;
  try{const value=(await takeReviewPrefetch(ref)) ?? await read(ref,'');if(token!==serial)return;set=value;if(!value.post_ids.length){await nextSet(ref);return;}const first=value.post_ids.findIndex((id:number)=>!value.decisions[id]);await show(first<0?0:first);}
  catch(e){if(token===serial)error=reviewError(e);}finally{if(token===serial)busy=false;}}
 async function show(n:number){
  const token=++serial;
  clearImage();pending=null;at=n;error='';
  // Navigation may supersede a read. Writes keep their existing busy guard.
  busy=false;loadingImage=true;
  const request=new AbortController();
  imageRequest=request;
  const signal=AbortSignal.any([request.signal,AbortSignal.timeout(12000)]);
  try{await imageAt(activeRef,n,token,signal);}
  catch(e){if(token===serial)error=reviewError(e);}
  finally{
   if(imageRequest===request){imageRequest=null;loadingImage=false;}
  }
 }
 async function saveGroup(answer:'selected'|'none'|'uncertain'){
  if(busy||editingPin||!image?.group||!imageReady)return;
  const token=serial,ref=activeRef,pid=set.post_ids[at];
  const selected=answer==='selected'?[...groupSelected].sort():[];
  if(answer==='selected'&&!selected.length)return;
  const key=JSON.stringify([answer,selected]);
  if(!pending||pending.key!==key)pending={key,request_id:crypto.randomUUID(),answer,selected,expected_context_sha256:image.group.context_sha256};
  const {key: _key, ...body}=pending;
  busy=true;error='';
  try{
   const r=await fetch(reviewRequestPath(MUT,ref,'/posts/'+pid+'/group-judgments'),
    {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:AbortSignal.timeout(12000)});
   if(token!==serial)return;
   if(!r.ok){
    if([401,403,409,422].includes(r.status)){pending=null;imageReady=false;}
    throw new Error(r.status===409?'審査の状態が変わりました。再読み込みしてください。':'保存できませんでした。同じ回答を再送できます。');
   }
   const value=await read(ref,'');if(token!==serial)return;
   pending=null;set=value;
   if(value.remaining===0){await nextSet(ref);return;}
   let next=value.post_ids.findIndex((id:number,i:number)=>i>at&&!value.decisions[id]);
   if(next<0)next=value.post_ids.findIndex((id:number)=>!value.decisions[id]);
   if(next>=0)await show(next);
  }catch(e){if(token===serial)error=reviewError(e);}
  finally{if(token===serial)busy=false;}
 }
 async function save(decision:string){if(busy||editingPin||!image||!imageUrl||!imageReady)return;const token=serial,ref=activeRef,pid=set.post_ids[at];busy=true;error='';
  if(!pending||pending.decision!==decision)pending={request_id:crypto.randomUUID(),decision,expected_context_sha256:image.context_sha256};
  try{const r=await fetch(reviewRequestPath(MUT,ref,'/posts/'+pid+'/judgments'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(pending),signal:AbortSignal.timeout(12000)});
   if(token!==serial)return;
   if([401,403,409,422].includes(r.status)){pending=null;imageReady=false;}
   if([401,403].includes(r.status))throw new Error('認証を確認して再読み込みしてください。判定は保存されていません。');
   if(r.status===422)throw new Error('回答の形式を受け付けられません。再読み込みしても続く場合は担当者へお知らせください。');
   if(!r.ok)throw new Error(r.status===409?'画像や判定が変わりました。再読み込みして確認してください。':'保存できませんでした。同じボタンで再送できます。');
   await r.json();if(token!==serial)return;const value=await read(ref,'');if(token!==serial)return;pending=null;set=value;if(value.remaining===0 || (value.adjudication && decision==='uncertain')){await nextSet(ref);return;}let next=value.post_ids.findIndex((id:number,i:number)=>i>at&&!value.decisions[id]);if(next<0)next=Math.min(at+1,value.post_ids.length-1);await show(next);
  }catch(e){if(token===serial)error=reviewError(e);}finally{if(token===serial)busy=false;}}
 // a finished set moves on by itself (owner 2026-11-22: 「次に進むべきなんじゃないか」): the next set with
 // unanswered images opens, or the dashboard when none is left
 async function nextSet(ref: string) {
  const token = serial;
  let to = '/tagger-dashboard#review';
  const pairedFrame = set?.paired_frame_sha256;
  try {
   const response = await fetch('/api/tagger-dashboard/review-queues', {
    cache: 'no-store', signal: AbortSignal.timeout(10000),
   });
   if (response.ok) {
    const queues = ((await response.json()).queues ?? []).filter((q: any) =>
      q.queue_sha256 !== ref && q.remaining > 0 && !q.request_unavailable
      && typeof q.unjudged_url === 'string' && q.unjudged_url.startsWith('/'));
    const next = (pairedFrame && queues.find((q: any) => q.frame_sha256 === pairedFrame)) || queues[0];
    if (next) to = next.unjudged_url;
   }
  } catch {}
  if (token === serial && ref === activeRef) await goto(to);
 }
 $effect(()=>{const ref=page.params.queue;if(!ref)return;untrack(()=>{activeRef=ref;set=null;void load(ref);});});
 onDestroy(()=>{serial++;clearImage();});
 function onKey(e:KeyboardEvent){
  if(enlargedReference||e.isComposing||e.ctrlKey||e.metaKey||e.altKey||!set)return;
  const target=e.target as HTMLElement;
  if(target&&(target.closest('.review-tile-tools')||target.isContentEditable||/^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)))return;
  const key=e.key.toLowerCase();
  if ((key==='4'||key==='5')&&canDiscard){
   e.preventDefault();
   if(!e.repeat)void discard(key==='5');
   return;
  }
  if(image?.group&&key.length>0){
   const index=key.length===1?groupKeys.indexOf(key):-1;
   const option=image.group.options[index];
   if(option||key==='enter'||key==='0'||key==='3'){
    e.preventDefault();
    if(e.repeat||busy||editingPin||!imageReady)return;
    if(option){
     if(!option.answered)chooseOption(option.tag);
    }else if(key==='enter')void saveGroup('selected');
    else void saveGroup(key==='0'?'none':'uncertain');
    return;
   }
  }else{
   const decision=({'1':'present','2':'absent','3':'uncertain'} as Record<string,string>)[key];
   if(decision){
    e.preventDefault();
    void save(decision);
    return;
   }
  }
  if(busy||editingPin)return;
  if(key==='arrowleft'&&at>0){e.preventDefault();void show(at-1);}
  else if(key==='arrowright'&&at<set.post_ids.length-1){e.preventDefault();void show(at+1);}
 }
</script>
{#snippet discardActions()}
    {#if canDiscard}
     <button class="exclude-image" aria-keyshortcuts="4" aria-label="学習対象から外す" disabled={busy||editingPin||!imageReady} onclick={()=>discard(false)}><span>学習除外 <kbd>4</kbd></span></button>
     <button class="delete-image" aria-keyshortcuts="5" aria-label="画像を削除" disabled={busy||editingPin||!imageReady} onclick={()=>discard(true)}><span>画像削除 <kbd>5</kbd></span></button>
    {/if}
{/snippet}
{#snippet tileTools(pid: number, current = false)}
 {#if !evaluationOnly && set}
  {@const pin = current ? image?.context.pin?.state : tilePins[pid]?.[set.tag]}
  {@const hasTag = current ? image?.context.tag_present : tilePosts[pid]?.tags?.some(tag => tag.names.includes(set.tag))}
  <div class="review-tile-tools">
   {#if category && tilePosts[pid]}
    <ThumbCategoryTags post={tilePosts[pid]} {category} focusTag={set.tag}
     pins={tilePins[pid] ?? null} deps={patchDeps} onUpdated={tileUpdated}
     onToggleLock={(tag) => toggleTilePin(pid, tag)} />
   {/if}
   {#if pin || hasTag !== undefined}
    <span class="target-pin" title={pin === 'present' ? 'タグを施錠済み' : pin === 'absent' ? 'タグを除外済み' : hasTag ? 'タグ有り・未施錠' : 'タグ無し・未固定'}
     aria-label={pin === 'present' ? 'タグを施錠済み' : pin === 'absent' ? 'タグを除外済み' : hasTag ? 'タグ有り・未施錠' : 'タグ無し・未固定'} role="img">
     {pin === 'present' ? '🔒' : pin === 'absent' ? '🚫' : hasTag ? '🔓' : '○'}
    </span>
   {/if}
  </div>
 {/if}
{/snippet}
{#snippet preview(n: number, label: string)}
 <div class="side">
  <button class="side-image" aria-label={label} disabled={busy||editingPin} onclick={()=>show(n)}><img use:thumbnailLoader={thumb(n)} src={thumb(n)} alt="" /></button>
  {@render tileTools(set.post_ids[n])}
 </div>
{/snippet}
<svelte:window onkeydown={onKey} />
<svelte:head><title>{set?.purpose==='candidate_evaluation'?'候補モデルの固定画像を確認':set?.purpose==='base_comparison'?'新旧モデル比較の画像を確認':set?.purpose==='training_support'?'学習用の画像を確認':'画像の判定を再確認'} · kura</title></svelte:head>
<main class="review-page" class:has-error={!!error}>
 <!-- manga-viewer layout (owner 2026-11-22: 「漫画ビューと同じにしろ」): one thin top bar, the image strip fills the
      rest of the window at full height, the answer bar at the bottom; the page never scrolls -->
 <Header onTogglePool={() => goto('/?mode=pools')} onToggleBulk={() => goto('/?bulk=1')} />
 <div class="topbar">
  <div class="tb-left"><a class="back" href="/tagger-dashboard#review" aria-label="確認一覧へ戻る">←</a>
   <h1>{set?.purpose==='candidate_evaluation'?'候補モデルの固定画像を確認':set?.purpose==='base_comparison'?'新旧モデル比較の画像を確認':set?.purpose==='training_support'?'学習用の画像を確認':'画像の判定を再確認'}</h1></div>
  {#if set}
   <!-- the question is the largest thing on the bar: which tag is being judged must be obvious at a glance -->
   <div class="task">この画像に <span class="tag-name">{set.tag}</span> はある？
    {#if image}<a class="tag-help" href={'/tags/edit?'+new URLSearchParams({tagId:String(image.context.tag_id),q:set.tag})} target="_blank" rel="noreferrer">説明</a>{/if}</div>
   <div class="tb-right">
    <nav aria-label="確認する画像"><button disabled={busy||editingPin||at===0} onclick={()=>show(at-1)} aria-label="前へ" aria-keyshortcuts="ArrowLeft">←</button><span class="pos">{at+1} <small>/ {set.post_ids.length}</small></span><button disabled={busy||editingPin||at===set.post_ids.length-1} onclick={()=>show(at+1)} aria-label="次へ" aria-keyshortcuts="ArrowRight">→</button></nav>
    <span class="review-response-count" role="status"><span class="chip">回答 {set.post_ids.length-set.remaining}/{set.post_ids.length}</span><span class="chip" class:todo={set.remaining>0}>未回答 {set.remaining}</span>{#if set.excluded_count}<span class="chip">対象外 {set.excluded_count}</span>{/if}<span class="chip">判別不能 {Object.values(set.decisions).filter(x=>x==='uncertain').length}</span>{#if set.remaining===0}<span class="chip done completion">完了</span>{/if}</span>
    {#if image && !evaluationOnly}<a href={'/post/'+set.post_ids[at]} target="_blank" rel="noreferrer">#{set.post_ids[at]}</a>{/if}
   <details class="review-notes">
    <summary>注意</summary>
    <div class="notes-body">
     {#if evaluationOnly}
      <p>画像を見て回答してください。{set.adjudication ? "別の判定者として回答を記録します。" : "回答は候補の評価用に保存します。"}既存タグや学習ラベルは変更しません。</p>
      <p>画像だけでは判断できない場合は「判別できない」を選んでください。この回答は陽性・陰性どちらにも数えません。</p>
     {:else}
      <p>「有る」「無い」はタグと固定を更新します。「判別できない」は既存の記録を維持します。</p>
     {/if}
     {#if set.purpose==='training_support'}<p>学習例を集めるための確認です。回答数だけでは必要な陽性・陰性のグループが揃ったとは判断できず、学習や公開を自動で開始しません。</p>{/if}
     {#if set.purpose==='candidate_evaluation' && !evaluationOnly}<p>現在のタグが見えるため盲検評価ではありません。「有る」「無い」は学習用GTに取り込まれ得ます。将来のモデルに対する独立評価には再利用できません。</p>{/if}
    </div>
   </details>
   </div>
  {/if}
 </div>
 <aside class="tag-definition" aria-label="タグの定義">
  {#if referencePosts.length}
   <div class="tag-reference-images" aria-label="タグの参考画像">

    <div class="tag-reference-strip">
     {#each referencePosts as post (post.id)}
      <button type="button" onclick={()=>enlargeReference(post)} aria-label={'参考画像 #' + post.id + ' を拡大'}>
       <img src={mediaUrl(post.thumbnailUrl)} alt="" />
      </button>
     {/each}
    </div>
   </div>
  {/if}
  {#if set?.tag_description?.trim() || set?.exclusive_tags?.length}
   <div class="tag-definition-text">
    {#if set?.exclusive_tags?.length}
     <div class="exclusive-rule">同時に付けないタグ <strong>{set.exclusive_tags.join('、')}</strong></div>
    {/if}
    {#if set?.tag_description?.trim()}<div>{set.tag_description}</div>{/if}
   </div>
  {/if}
 </aside>
 <dialog bind:this={referenceDialog} class="reference-dialog" aria-label="参考画像の拡大" onclose={()=>enlargedReference=null}>
  {#if enlargedReference}
   <div class="reference-dialog-actions">
    <a href={'/post/' + enlargedReference.id} target="_blank" rel="noreferrer">投稿を開く</a>
    <button type="button" onclick={()=>referenceDialog.close()}>閉じる</button>
   </div>
   <img src={MUT + '/post/' + enlargedReference.id + '/content'} alt={'タグの参考画像 #' + enlargedReference.id} />
  {/if}
 </dialog>
 {#if error && !set}<p role="alert">{error}</p><button onclick={()=>load()} disabled={busy}>再読み込み</button>{/if}
 {#if set?.post_ids.length}
  <div class="image-stage">
   <div class="preview-strip prev">
    {#each [3,2,1] as distance}
     {#if at>=distance}{@render preview(at-distance, '前の画像 #' + set.post_ids[at-distance])}{/if}
    {/each}
   </div>
   <div class="main" aria-busy={loadingImage||busy}>{#if loadingImage}<p role="status">画像を読み込んでいます…</p>{/if}{#if imageUrl}<img src={imageUrl} alt="判定対象の画像" onload={(event)=>displayResult(event,true)} onerror={(event)=>displayResult(event,false)} />{/if}
    {#if image}{@render tileTools(set.post_ids[at], true)}{/if}
   </div>
   <div class="preview-strip next">
    {#each [1,2,3] as distance}
     {#if at+distance<set.post_ids.length}{@render preview(at+distance, '次の画像 #' + set.post_ids[at+distance])}{/if}
    {/each}
   </div>
  </div>
  <div class="decision-bar" aria-label="画像への回答">
   {#if error}<div class="bar-error"><p role="alert">{error}</p><button onclick={()=>load()} disabled={busy}>再読み込み</button></div>{/if}
   <span class="decision-status" role="status">{busy?'保存・読み込み中…':image?.previous_judgment?'今回の回答: '+names[image.previous_judgment.decision]:''}</span>
   {#if image?.group}
    <div class="group-options" aria-label="該当するタグ">
     {#each image.group.options as option, index (option.tag)}
      {@const hotkey=groupKeys[index]?.toUpperCase()}
      <button type="button" aria-pressed={groupSelected.includes(option.tag)} aria-keyshortcuts={hotkey}
       disabled={busy||editingPin||!imageReady||option.answered}
       onclick={()=>chooseOption(option.tag)}>
       <strong>{option.tag} {#if hotkey}<kbd>{hotkey}</kbd>{/if}</strong>
       {#if option.definition}<span>{option.definition}</span>{/if}
       {#if option.answered}<small>回答済み</small>{/if}
      </button>
     {/each}
    </div>
    <div class="actions" class:with-discard={canDiscard}>
     <button class="yes" aria-keyshortcuts="Enter" disabled={busy||editingPin||!imageReady||!groupSelected.length} onclick={()=>saveGroup('selected')}>選んだタグで確定 <kbd>Enter</kbd></button>
     <button class="no" aria-keyshortcuts="0" disabled={busy||editingPin||!imageReady} onclick={()=>saveGroup('none')}>どれでもない <kbd>0</kbd></button>
     <button class="unsure" aria-keyshortcuts="3" disabled={busy||editingPin||!imageReady} onclick={()=>saveGroup('uncertain')}>わからない <kbd>3</kbd></button>
     {@render discardActions()}
    </div>
   {:else}
   <div class="actions" class:with-discard={canDiscard}>
    <button class="yes" aria-keyshortcuts="1" aria-label={evaluationOnly ? "有る — 評価用の回答を保存" : "有る — タグを付けて確定"} aria-pressed={image?.previous_judgment?.decision==='present'} disabled={busy||editingPin||!imageReady} onclick={()=>save('present')}><span>有る <kbd>1</kbd></span></button>
    <button class="no" aria-keyshortcuts="2" aria-label={evaluationOnly ? "無い — 評価用の回答を保存" : "無い — タグを外して確定"} aria-pressed={image?.previous_judgment?.decision==='absent'} disabled={busy||editingPin||!imageReady} onclick={()=>save('absent')}><span>無い <kbd>2</kbd></span></button>
    <button class="unsure" aria-keyshortcuts="3" aria-label="判別できない — 今回は未確定" aria-pressed={image?.previous_judgment?.decision==='uncertain'} disabled={busy||editingPin||!imageReady} onclick={()=>save('uncertain')}><span><span class="wide-uncertain">判別できない</span><span class="short-uncertain">保留</span> <kbd>3</kbd></span></button>
    {@render discardActions()}
   </div>
   {/if}
  </div>
 {/if}
</main>
<style>
 .group-options{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,10rem),1fr));gap:.3rem;max-height:24dvh;overflow:auto}
 .group-options button{display:grid;text-align:left;gap:.2rem;font-size:.85em;padding:.3rem .45rem}
 .group-options button span,.group-options small{font-size:.72em;line-height:1.2}
 .group-options button[aria-pressed=true]{outline:2px solid #ffd25a}
 .review-page{display:flex;flex-direction:column;height:100dvh;overflow:hidden;background:#000;color:var(--text,#ddd);font-size:20px}
 .topbar{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(max-content,1fr);align-items:center;gap:.5em 1.2em;padding:.45em 1em;background:#16181b;border-bottom:2px solid #333;flex:0 0 auto}
 .tb-left{display:flex;align-items:center;gap:.7em;min-width:0}.tb-left h1{font-size:1em;font-weight:500;margin:0;color:#aaa;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .back{font-size:1.4em;text-decoration:none;color:#ddd}
 .task{font-size:1.5em;font-weight:600;color:#eee;white-space:nowrap;display:flex;align-items:center;gap:.4em}
 .tag-name{font-size:1.15em;font-weight:800;color:#111;background:#ffd25a;padding:.05em .45em;border-radius:.3em;font-family:ui-monospace,monospace}
 .tag-help{font-size:.55em;font-weight:400}
 .tb-right{display:flex;align-items:center;justify-content:flex-end;gap:.6em;flex-wrap:nowrap;white-space:nowrap}
 .topbar a{color:#8cb8e8}
 .topbar nav{display:flex;align-items:center;gap:.4em}.topbar nav button{min-height:1.9em;min-width:2.2em;font-size:1.1em;cursor:pointer}
 .pos{font-size:1.4em;font-weight:700;color:#fff}.pos small{font-size:.65em;color:#aaa;font-weight:400}
 .review-response-count{display:flex;gap:.35em}
 .chip{display:inline-block;font-size:.95em;padding:.15em .6em;border-radius:1em;background:#2c3035;color:#e8e8e8;white-space:nowrap}
 .chip.todo{background:#5a4a14;color:#ffe08a}.chip.done{background:#1d4d2b;color:#bff0c8}
.review-notes{position:relative}.review-notes summary{cursor:pointer}
 .notes-body{position:absolute;right:0;top:1.6rem;z-index:20;width:min(36em,90vw);background:var(--surface,#1b1d20);border:1px solid var(--line,#444);border-radius:6px;padding:.4rem .7rem}
 .notes-body p{margin:.4rem 0;line-height:1.45}
 .tag-definition{--reference-height:clamp(4.25rem,13dvh,7rem);box-sizing:border-box;block-size:calc(var(--reference-height) + 1.25rem);display:flex;align-items:center;justify-content:center;gap:.6rem;flex:none;padding:.35rem .65rem;background:var(--surface,#1b1d20);font-size:.8em;line-height:1.4;text-align:center}
 .tag-definition-text{min-width:0;flex:0 1 auto;max-width:42rem;max-height:100%;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}
 .exclusive-rule{color:#ffd25a;margin-bottom:.25rem}
 .tag-reference-images{min-width:0;flex:0 0 auto;max-width:100%;overflow-x:auto}.tag-reference-images:has(+.tag-definition-text){max-width:58%}
 .tag-reference-strip{display:flex;justify-content:center;gap:.4rem;width:max-content;margin:.15rem auto}
 .tag-reference-strip button{display:block;flex:none;width:clamp(5rem,19vw,9rem);height:var(--reference-height);padding:0;border:0;background:none;cursor:zoom-in}
 .tag-reference-strip img{display:block;width:100%;height:100%;object-fit:contain;background:#0f1012;border-radius:4px}
 .reference-dialog{max-width:96vw;max-height:94dvh;padding:.5rem;background:#16181b;color:#eee;border:1px solid #666}
 .reference-dialog::backdrop{background:#000c}
 .reference-dialog img{display:block;max-width:90vw;max-height:80dvh;object-fit:contain;margin:auto}
 .reference-dialog-actions{display:flex;justify-content:flex-end;align-items:center;gap:1em;margin-bottom:.4rem}
 .reference-dialog-actions a{color:#8cb8e8}
 .image-stage{flex:1 1 0;min-height:0;display:grid;grid-template-columns:minmax(0,1fr) max-content minmax(0,1fr);grid-template-rows:minmax(0,1fr);container-type:inline-size;overflow:hidden}
 .image-stage .main{grid-area:1/2;position:relative;display:flex;justify-content:center;max-width:62cqw;height:100%;min-width:0}
 .main img{display:block;height:100%;width:auto;min-width:0;max-width:100%;object-fit:contain}
 .side{position:relative;flex:0 1 auto;min-width:0;height:100%;overflow:hidden}
 .side:has(:global(img[data-thumbnail-state="unavailable"])){display:none}
 .side-image{width:100%;height:100%;padding:0;border:0;background:none;opacity:.76;display:flex;justify-content:center;overflow:hidden;cursor:pointer;transition:opacity .15s}
 .preview-strip:not(:has(.side-image)){display:none}
 .preview-strip.prev{grid-area:1/1;justify-content:flex-end}
 .preview-strip.next{grid-area:1/3;justify-content:flex-start}
 .preview-strip{display:flex;min-width:0;min-height:0}.side-image:hover:not(:disabled),.side-image:focus-visible,.side:focus-within .side-image{opacity:.85}
 .side img{height:100%;width:auto;max-width:100%;min-width:0;object-fit:contain;display:block}
 .preview-strip.prev img{object-position:right center}
 .preview-strip.next img{object-position:left center}
 .review-tile-tools{position:absolute;inset:0;pointer-events:none}
 .target-pin{position:absolute;right:.25rem;bottom:.25rem;padding:.08rem .25rem;background:#000a;border-radius:4px;font-size:.8rem;pointer-events:auto}
 .decision-bar{position:relative;flex:0 0 auto;background:var(--bg,#181818);border-top:1px solid var(--border,#555);padding:.4rem .75rem max(.45rem,env(safe-area-inset-bottom))}
 .decision-status{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
 .actions{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.5rem;max-width:75em;margin:0 auto}
 .actions.with-discard{grid-template-columns:repeat(5,minmax(0,1fr))}
 /* readable answer buttons (owner 2026-11-22: grey text on grey could not be read) */
 .actions button{display:flex;flex-direction:column;gap:.15rem;align-items:center;justify-content:center;min-height:2.35em;line-height:1.3;cursor:pointer;
  color:#fff;font-size:clamp(.65rem,2.85vw,1.25rem);font-weight:600;white-space:nowrap;border:2px solid #666;border-radius:6px;background:#2a2d31}
 .actions .yes{background:#174d39;border-color:#62c299}
 .actions .no{background:#343648;border-color:#a3a6bf}
 .actions .unsure{background:#584017;border-color:#e0b35b}
 .actions .exclude-image{background:#173d64;border-color:#7eb7ef}
 .actions .delete-image{background:#641f29;border-color:#ff8392}
 .actions button:hover:not(:disabled){filter:brightness(1.25)}.actions button:disabled{opacity:.45}
 .actions small{font-size:.8em;font-weight:400;color:#e6e6e6}.actions button[aria-pressed=true]{outline:3px solid #83bfff;outline-offset:1px}

 .bar-error{display:flex;align-items:center;gap:.5rem;margin-bottom:.4rem}.bar-error p{margin:0;flex:1;max-height:5em;overflow:auto}
 button:disabled{cursor:default}button:focus-visible,a:focus-visible,summary:focus-visible,[tabindex]:focus-visible{outline:3px solid #83bfff;outline-offset:2px}
 kbd{font-size:.8em;padding:0 .35rem;border:1px solid #ddd;border-radius:3px;color:#fff;background:#0006}
 .short-uncertain{display:none}
 @media(max-width:520px){.actions.with-discard{gap:.2rem}.actions.with-discard button{padding-inline:0}.actions.with-discard kbd{padding-inline:.12rem}.with-discard .wide-uncertain{display:none}.with-discard .short-uncertain{display:inline}}
 @media(max-width:600px){.image-stage .main{max-width:42cqw}.topbar{grid-template-columns:1fr;font-size:.9em}.tb-right{justify-content:flex-start}}
</style>
