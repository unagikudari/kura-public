import test from 'node:test';
import assert from 'node:assert/strict';
import { transferOf, recordFetches } from '../src/lib/paintTiming.ts';

const on = { observing: true, lostUntil: -1 };
const img = (url, complete = true) => ({ url, complete });

test('a view drawn from held images reports no transfer, not an older fetch', () => {
  const earlier = [{ url: 'a', start: 100, end: 300, proto: 'h3' }];
  assert.deepEqual(transferOf([img('a')], earlier, 5_000, 5_400, on), { proto: 'none', fetched: 0, held: 1, pending: 0 });
});

test('only the images in view count, with every protocol among their fetches in the span', () => {
  const list = [
    { url: 'a', start: 4_900, end: 5_100, proto: 'h2' },   // begun before, finished inside
    { url: 'b', start: 5_200, end: 5_350, proto: 'h3' },
    { url: 'prefetch', start: 5_200, end: 5_300, proto: 'h3' },  // not in view: ignored
  ];
  assert.deepEqual(transferOf([img('a'), img('b'), img('c')], list, 5_000, 5_400, on),
    { proto: 'h2+h3', fetched: 2, held: 1, pending: 0 });
  // a prefetch alone does not make a held view look fetched
  assert.equal(transferOf([img('c')], list, 5_000, 5_400, on).proto, 'none');
});

test('what was not observed is not called none', () => {
  assert.equal(transferOf([img('a')], [], 0, 10, { observing: false, lostUntil: -1 }).proto, 'unobserved');
  assert.deepEqual(transferOf([img('a', false)], [], 0, 10, on), { proto: 'pending', fetched: 0, held: 0, pending: 1 });
  assert.equal(transferOf([img('a')], [{ url: 'a', start: 1, end: 2, proto: '' }], 0, 10, on).proto, 'unknown');
});

test('past the bound the oldest fetches go, and a span reaching into them is marked partial', () => {
  const entry = (i, proto) => ({ initiatorType: 'img', name: `u${i}`, startTime: i, responseEnd: i + 0.5, nextHopProtocol: proto });
  recordFetches([{ initiatorType: 'script', name: 's', startTime: 0, responseEnd: 1, nextHopProtocol: 'h2' }]);
  const { fetches, lostUntil } = recordFetches(Array.from({ length: 501 }, (_, i) => entry(i, i < 300 ? 'h2' : 'h3')));
  assert.equal(fetches.length, 250);
  assert.equal(lostUntil, 250.5);                       // u0..u250 dropped
  const t = transferOf([img('u100'), img('u400')], fetches, 0, 1_000, { observing: true, lostUntil });
  assert.equal(t.partial, true);
  assert.equal(transferOf([img('u400')], fetches, 300, 1_000, { observing: true, lostUntil }).partial, undefined);
});

test('a fetch still queued in the observer when a measurement ends counts as fetched, not held', async () => {
  const { observeFetches, flushFetches } = await import('../src/lib/paintTiming.ts?queued');
  const queued = [{ initiatorType: 'img', name: 'fresh', startTime: 5_100, responseEnd: 5_200, nextHopProtocol: 'h3' }];
  class Observer {             // delivers nothing on its own; holds the entry until takeRecords()
    constructor(callback) { this.callback = callback; }
    observe() {}
    takeRecords() { return queued.splice(0); }
  }
  assert.equal(observeFetches(Observer), true);
  const state = flushFetches();
  assert.deepEqual(transferOf([img('fresh')], state.fetches, 5_000, 5_400, state),
    { proto: 'h3', fetched: 1, held: 0, pending: 0 });
});

test('a browser without resource timing reads unobserved', async () => {
  const { observeFetches, flushFetches } = await import('../src/lib/paintTiming.ts?none');
  assert.equal(observeFetches(class { constructor() { throw new Error('no'); } }), false);
  const state = flushFetches();
  assert.equal(transferOf([img('a')], state.fetches, 0, 10, state).proto, 'unobserved');
});
