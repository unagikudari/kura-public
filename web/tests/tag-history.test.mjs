import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';
const source = await readFile(new URL('../src/lib/tagHistory.ts', import.meta.url), 'utf8');
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const { pushTagHistory, BULK_TAG_HISTORY_MAX } = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));

test('the newest tag comes first and a reused tag moves to the front', () => {
  assert.deepEqual(pushTagHistory(['a', 'b', 'c'], 'b'), ['b', 'a', 'c']);
  assert.deepEqual(pushTagHistory([], ' penis '), ['penis']);
});

test('history keeps five tags and ignores empty names', () => {
  let h = [];
  for (const t of ['1', '2', '3', '4', '5', '6']) h = pushTagHistory(h, t);
  assert.equal(BULK_TAG_HISTORY_MAX, 5);
  assert.deepEqual(h, ['6', '5', '4', '3', '2']);
  assert.equal(pushTagHistory(h, '  '), h);
});

test('ten are kept so five remain after hiding the target and three focus tags', () => {
  let h = [];
  for (const t of 'abcdefghijkl') h = pushTagHistory(h, t, 10);
  assert.equal(h.length, 10);
  assert.deepEqual(h.slice(4, 9), ['h', 'g', 'f', 'e', 'd']);
});
