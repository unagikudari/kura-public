import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';
const source = await readFile(new URL('../src/lib/historyWindow.ts', import.meta.url), 'utf8');
const javascript = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022}}).outputText;
const { rememberWindow: save, recallWindow: read } = await import('data:text/javascript;base64,' + Buffer.from(javascript).toString('base64'));
test('history entries with the same URL keep independent copied windows', () => {
 const rows=[{id:7,tags:['before']}];
 const first=save('key',30,10,rows,0); rows[0].tags.push('later');
 const second=save('key',30,20,[{id:8}],0);
 assert.deepEqual(read(first,'key',30,1),{offset:10,rows:[{id:7,tags:['before']}]});
 read(first,'key',30,1).rows[0].tags.push('edited');
 assert.deepEqual(read(first,'key',30,1).rows[0].tags,['before']);
 assert.equal(read(second,'key',30,1).offset,20);
});
test('different safe/search/account/sort identity, changed extent and expiry refuse reuse', () => {
 const token=save('safe-account-sort',30,0,[{id:1}],0);
 assert.equal(read(token,'other-account-sort',30,1),null);
 assert.equal(read(token,'safe-account-sort',31,1),null);
 assert.equal(read(token,'safe-account-sort',30,600001),null);
});
test('entry count, row count and serialized memory are bounded', () => {
 const first=save('bounded',1000,0,[{id:1}],0);
 for(let i=0;i<6;i++)save('bounded',1000,i,[{id:i+2}],0);
 assert.equal(read(first,'bounded',1000,1),null);
 const token=save('rows',1000,10,Array.from({length:800},(_,id)=>({id})),0);
 assert.equal(read(token,'rows',1000,1).rows.length,400);
 assert.equal(save('oversize',1,0,[{value:'x'.repeat(1000001)}],0),null);
 assert.equal(save('empty',30,0,[null,null],0),null);
});
