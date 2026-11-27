import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';
import {compile} from 'svelte/compiler';
const page=readFileSync(new URL('../src/routes/post/[id]/+page.svelte',import.meta.url),'utf8');
const image=readFileSync(new URL('../src/lib/components/MangaImage.svelte',import.meta.url),'utf8');
const handler=page.slice(page.indexOf('  async function onItemDblClick'),page.indexOf('  // ロード済みページに対する全選択'));
function world(tagged=false, options={}) {
 const post={id:1,tags:tagged?['target']:[]}; const calls=[];
 const s={ $verifyTarget:'target',$verifyHeaderOn:true,vApplying:false,suppressItemClick:false,
 vPinStates:{},vAddIds:new Set([2]),vRemoveIds:new Set([3]),vResult:null,vProgress:null,
 posts:[post],patchDeps:{},postHasTag:(p,t)=>p.tags.includes(t),
 window:{confirm:()=>true},pm:{lockedRemoveConfirm:()=>''},applyUpdated:p=>s.posts=[p],
 applyBulkTag:async(t,add,remove,deps)=>{calls.push({t,add,remove});deps.onUpdated({...post,tags:add.length?[t]:[]});return {fail:0};}, ...options};
 vm.createContext(s);vm.runInContext(ts.transpileModule(handler,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText,s);
 return {s,post,calls};
}
test('add and remove only the double-clicked image, update pins, preserve other selections',async()=>{
 for(const tagged of [false,true]) {const {s,post,calls}=world(tagged);await s.onItemDblClick(post);
 assert.equal(calls.length,1);assert.equal(calls[0][tagged?'remove':'add'][0],1);
 assert.equal(s.vPinStates[1].target,tagged?'absent':'present');assert.ok(s.vAddIds.has(2));assert.ok(s.vRemoveIds.has(3));assert.equal(s.vApplying,false);}
});
test('normal viewing, empty target, busy, drag and declined locked removal do not write',async()=>{
 for(const opts of [{$verifyHeaderOn:false},{$verifyTarget:''},{vApplying:true},{suppressItemClick:true},{vPinStates:{1:{target:'present'}},window:{confirm:()=>false}}]){
 const {s,post,calls}=world(true,opts);await s.onItemDblClick(post);assert.equal(calls.length,0);}
});
test('failure releases busy state without inventing a pin or clearing selections',async()=>{
 const {s,post}=world(false,{applyBulkTag:async()=>({fail:1})});await s.onItemDblClick(post);
 assert.equal(s.vResult.fail,1);assert.equal(s.vPinStates[1],undefined);assert.equal(s.vApplying,false);assert.ok(s.vAddIds.has(2));
});
test('Svelte wiring reaches handler; retry doubleclick cannot toggle tags',()=>{
 assert.match(page,/ondblclick=\{\(\) => onItemDblClick\(p\)\}/);
 assert.match(image,/\{onclick\} \{ondblclick\}/);assert.match(image,/class="retry" ondblclick=\{\(e\) => e.stopPropagation\(\)\}/);
 compile(image,{filename:'MangaImage.svelte',generate:'client'});compile(page,{filename:'page.svelte',generate:'client'});
});

test('header bulk button toggles manga state without navigating to the grid',()=>{
 assert.match(page,/bulkMode=\{\$verifyHeaderOn\}\s+onToggleBulk=\{toggleVerifyHeader\}/);
 const toggle=page.slice(page.indexOf('  function toggleVerifyHeader()'),page.indexOf('  // x ='));
 assert.match(toggle,/verifyHeaderOn.update/);assert.match(toggle,/anchorFocused/);assert.doesNotMatch(toggle,/goto\(/);
});
