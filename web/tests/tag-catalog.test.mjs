import assert from 'node:assert/strict';
import test from 'node:test';
import { catalogParams, catalogReturn, readTagCleanup } from '../src/lib/tagCatalog.ts';

test('catalog context round-trips reserved characters and explicit display/filter', () => {
  const p = catalogParams('  a/b & 日本語  ', 'artist', 'no_alias', 'list');
  const restored = new URLSearchParams(p.toString());
  assert.equal(restored.get('q'), 'a/b & 日本語');
  assert.equal(restored.get('cat'), 'artist');
  assert.equal(restored.get('cleanup'), 'no_alias');
  assert.equal(restored.get('view'), 'list');
  assert.equal(catalogParams('', '', 'all').toString(), '');
});

test('editor returns only to local tag management and normalizes unknown filters', () => {
  assert.equal(catalogReturn('/tags/manage?q=a%2Fb&view=list'), '/tags/manage?q=a%2Fb&view=list');
  for (const value of [null, 'https://example.test/tags/manage', '//example.test/tags/manage', '/tags/edit', 'http://[']) {
    assert.equal(catalogReturn(value), '/tags/manage');
  }
  assert.equal(catalogReturn('/artists?id=101'), '/artists?id=101');
  assert.equal(catalogReturn('https://example.test/artists'), '/tags/manage');
  assert.equal(catalogReturn('/artists/other'), '/tags/manage');
  assert.equal(readTagCleanup('unused'), 'unused');
  assert.equal(readTagCleanup('no_alias'), 'no_alias');
  assert.equal(readTagCleanup('unknown'), 'all');
});
