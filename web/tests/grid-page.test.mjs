import assert from 'node:assert/strict';
import test from 'node:test';
import { assertCompletePage } from '../src/lib/gridPage.ts';

test('complete pages and a shrinking tail are accepted without estimated counts', () => {
  assertCompletePage({total:5,results:[{id:3},{id:4}]},3,40);
  assertCompletePage({total:2,results:[]},40,40);
  assertCompletePage({total:0,results:[]},0,40);
});
test('short, oversized, duplicate and invalid results never enter the sparse cache', () => {
  for (const page of [{total:5,results:[{id:1}]},{total:1,results:[{id:1},{id:2}]},
    {total:2,results:[{id:1},{id:1}]},{total:1,results:[{id:0}]},{total:-1,results:[]},{total:NaN,results:[]}]) {
    assert.throws(() => assertCompletePage(page,0,40));
  }
});
