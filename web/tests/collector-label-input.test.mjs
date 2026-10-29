import assert from 'node:assert/strict';
import test from 'node:test';
import { parseTagInput } from '../src/lib/collector/api.ts';

test('tag field separates normalized delimiters while preserving spaces inside labels', () => {
  assert.deepEqual(parseTagInput('旅行，未整理、 summer vacation\n旅行'), ['旅行', '未整理', 'summer vacation']);
  assert.deepEqual(parseTagInput('  ,、\n'), []);
});
