import assert from 'node:assert/strict';
import test from 'node:test';
import { planImport,matchImportFiles } from '../src/lib/collector/importOperations.ts';
const input=(name,bytes,path)=>({file:new File([bytes],name),relativePath:path});
test('durable plans hash original bytes and reselection preserves receipt identity',async()=>{
 const files=[input('a.png','same','Book/a.png'),input('a.png','same','Book/sub/a.png')];
 const plan=await planImport(files,null,()=>{});
 assert.equal(plan.items[0].sha256,plan.items[1].sha256);
 assert.notEqual(plan.items[0].request_key,plan.items[1].request_key);
 const manifest=plan.items.map((i,n)=>({...i,position:n+1,state:n?'pending':'registered',collection_id:7}));
 const result=await matchImportFiles(manifest,files,()=>{});
 assert.equal(result.missing,0);assert.equal(result.matched[1].item.request_key,plan.items[1].request_key);
 const subset=await matchImportFiles(manifest,[files[0],input('other.png','unused')],()=>{});
 assert.equal(subset.missing,1);assert.equal(subset.ignored,1);
 await assert.rejects(()=>matchImportFiles(manifest,[input('a.png','diff','Book/a.png')],()=>{}),/内容が保存済み/);
});
