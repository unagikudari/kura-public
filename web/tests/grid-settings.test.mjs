import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';
const source=await readFile(new URL('../src/lib/gridSettings.ts',import.meta.url),'utf8');
const js=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2022}}).outputText;
const {gridScope,gridCookie,defaultGridSettings,readGridSettings,legacyGridSettings}=await import('data:text/javascript;base64,'+Buffer.from(js).toString('base64'));
test('global queries, pool contents and pool listings have independent settings',()=>{
 assert.equal(gridScope('artist'), 'search');assert.equal(gridScope('pool:42 tag'),'pool');assert.equal(gridScope('pool:42','pools'),'pools');
 assert.equal(new Set(['search','pool','pools'].map(gridCookie)).size,3);
 assert.equal(defaultGridSettings('pool').sort,'page');assert.equal(defaultGridSettings('pool').dir,'asc');
 assert.equal(defaultGridSettings('search').sort,'id');assert.equal(defaultGridSettings('search').dir,'desc');
});
test('stored page ordering cannot leak into global or pool-list queries',()=>{
 const raw=JSON.stringify({version:1,sort:'page',dir:'desc',cols:8,open:'manga'});
 assert.equal(readGridSettings(raw,'pool').sort,'page');
 assert.equal(readGridSettings(raw,'search').sort,'id');
 assert.equal(readGridSettings(raw,'pools').sort,'id');
 assert.equal(readGridSettings(raw,'pool').cols,8);assert.equal(readGridSettings(raw,'pool').open,'manga');
});
test('malformed and unsupported input falls back without persisting arbitrary values',()=>{
 for(const raw of ['',null,'{','null','[]','{"version":2}'])assert.equal(readGridSettings(raw,'pool'),null);
 assert.deepEqual(readGridSettings('{"version":1,"sort":"random","dir":"sideways","cols":1000,"open":"unsafe"}','pool'),defaultGridSettings('pool'));
 assert.equal(readGridSettings('x'.repeat(1025),'pool'),null);
});

test('legacy shared density/click mode migrates without importing pool-list sorting into pool contents',()=>{
 const values={kura_cols:'6',kura_open_mode:'manga',kura_pool_sort:'updated',kura_pool_dir:'desc'};
 const storage={getItem:key=>values[key]??null};
 assert.deepEqual(legacyGridSettings('pool',storage),{version:1,sort:'page',dir:'asc',cols:6,open:'manga'});
 assert.deepEqual(legacyGridSettings('pools',storage),{version:1,sort:'updated',dir:'desc',cols:6,open:'manga'});
 assert.deepEqual(legacyGridSettings('search',{getItem:()=>{throw Error('blocked')}}),defaultGridSettings('search'));
});
