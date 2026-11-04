import test from 'node:test';
import assert from 'node:assert/strict';
import { splitPoolName, poolDisplayName, joinParts } from '../src/lib/poolName.ts';

// A made-up site: the account id is "acct <digits>". Real sites' rules live in the site adapter.
const rules = [{ label: 'example', find(name) {
  const m = /acct (\d+)/.exec(name);
  return m ? { start: m.index, end: m.index + m[0].length, url: `https://example.invalid/u/${m[1]}` } : null;
} }];

test('an account id the site rules find is split out, never left in the shown name', () => {
  assert.deepEqual(splitPoolName('Artist acct 12345678 title', rules),
    { before: 'Artist', account: { label: 'example', url: 'https://example.invalid/u/12345678' }, after: 'title' });
  assert.equal(poolDisplayName('Artist acct 12345678 title', rules), 'Artist title');
  assert.equal(poolDisplayName('[Artist acct 12345678] title', rules), '[Artist] title');
});

test('without site rules a name is shown as it is', () => {
  for (const name of ['Artist acct 12345678', 'plain name']) {
    assert.deepEqual(splitPoolName(name, []), { before: name, account: null, after: '' });
    assert.equal(poolDisplayName(name, []), name);
  }
});

test('a name that was only the id reads as the site label; a bracket closes without a space', () => {
  assert.equal(poolDisplayName('acct 12345678', rules), 'example');
  assert.equal(joinParts('[a', '] b'), '[a] b');
  assert.equal(joinParts('a', 'b'), 'a b');
});
