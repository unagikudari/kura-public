import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';
const source = await readFile(new URL('../src/lib/navigationLinks.ts', import.meta.url), 'utf8');
const javascript = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.ESNext}}).outputText;
const { parseNavigationLinks: parse } = await import('data:text/javascript;base64,' + Buffer.from(javascript).toString('base64'));
test('no configuration and invalid configuration create no private defaults', () => {
  for (const value of [undefined, '', '{', 'null', '{}', '"wrong"']) assert.deepEqual(parse(value), []);
});
test('operator links preserve order and allow relative local navigation', () => {
  assert.deepEqual(parse(JSON.stringify([{label:' Models ',url:'https://models.example.invalid'}, {label:'Help',url:'/help'}])),
    [{label:'Models',url:'https://models.example.invalid/'},{label:'Help',url:'/help'}]);
});
test('unsafe schemes, credentials, malformed entries, and scheme-relative links are refused', () => {
  const urls=['javascript:alert(1)','data:text/html,test','file:///etc/passwd','https://user:secret@example.invalid','//external.invalid','/\\external.invalid','/\nexternal.invalid','relative/path'];
  assert.deepEqual(parse(JSON.stringify(urls.map(url=>({label:'bad',url})))), []);
  assert.deepEqual(parse(JSON.stringify([null, {label:4,url:'https://example.invalid'},{label:'',url:'/safe'}])),[]);
});
test('menu contribution is bounded', () => {
  assert.equal(parse(JSON.stringify(Array.from({length:30},()=>({label:'Help',url:'/help'})))).length,20);
});
