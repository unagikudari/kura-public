import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';
const source=await readFile(new URL('../src/lib/mangaPrefetch.ts',import.meta.url),'utf8');
const js=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText;
const {mangaNear}=await import('data:text/javascript;base64,'+Buffer.from(js).toString('base64'));
test('forward and reverse acquisition prioritize the next two pages with one page behind',()=>{
 for(const direction of [1,-1]){
  const active=Array.from({length:41},(_,i)=>i).filter(i=>mangaNear(i,20,direction));
  assert.deepEqual(active,direction===1?[19,20,21,22]:[18,19,20,21]);
 }
});
test('edges never demand additional pages outside the collection',()=>{
 assert.deepEqual([0,1,2,3].filter(i=>mangaNear(i,0,1)),[0,1,2]);
 assert.deepEqual([0,1,2,3].filter(i=>mangaNear(i,3,-1)),[1,2,3]);
});
