import test from 'node:test';
import assert from 'node:assert/strict';
import { installActivityFeed, ownPosts } from '../src/lib/activityFeed.ts';

const m = { common: { notify: { postsAdded: (n, pool) => `${n}@${pool}`, postsAddedLoose: n => `${n}@none` } } };
const T0 = '2026-11-01T00:00:00+00:00';
const at = s => new Date(Date.parse(T0) + s * 1000).toISOString();

function fixture(t, answers) {
  const saved = Object.getOwnPropertyDescriptor(globalThis, 'document');
  const document = Object.assign(new EventTarget(), { visibilityState: 'visible' });
  Object.defineProperty(globalThis, 'document', { configurable: true, value: document });
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const asked = [], shown = [];
  let hold = null;
  const fetchFn = async (url, { signal } = {}) => {
    asked.push(url);
    if (hold) await new Promise((resolve, reject) => { hold.release = resolve; signal?.addEventListener('abort', () => reject(new Error('aborted'))); });
    return { ok: true, json: async () => answers.shift() ?? { now: at(600), events: [] } };
  };
  const stop = installActivityFeed('', m, toast => shown.push(toast), fetchFn);
  t.after(() => { stop(); if (saved) Object.defineProperty(globalThis, 'document', saved); else delete globalThis.document; });
  const settle = async () => { for (let i = 0; i < 5; i++) { await new Promise(r => setImmediate(r)); t.mock.timers.tick(0); } };
  return { document, asked, shown, settle, stop, holdNext: () => (hold = {}), hold: () => hold };
}
const posts = (pool, ids, name = 'P') => ({ key: `posts:${pool}:${ids.at(-1)}`, kind: 'posts_added', level: 'info', pool_id: pool, pool_name: name, post_ids: ids });

test('the overlap never reaches before the tab opened, and nothing is shown twice', async t => {
  const { asked, shown, settle } = fixture(t, [
    { now: T0, events: [] },
    { now: at(20), events: [posts(5, [3]), { key: 'ext:a', kind: 'extension', level: 'error', text: 'failed', href: '/ext' }] },
    { now: at(40), events: [posts(5, [3, 4]), { key: 'ext:a', kind: 'extension', level: 'error', text: 'failed' }] },
  ]);
  await settle();
  assert.equal(asked[0], '/api/activity');
  t.mock.timers.tick(20_000); await settle();
  assert.match(asked[1], /since=2026-11-01T00%3A00%3A00/);        // start, not two minutes before it
  assert.deepEqual(shown.map(s => s.text), ['1@P', 'failed']);
  assert.equal(shown[0].href, '/?q=pool:5');
  t.mock.timers.tick(20_000); await settle();
  assert.match(asked[2], /since=2026-11-01T00%3A00%3A00/);        // 20 s minus 2 min, held at start
  assert.deepEqual(shown.map(s => s.text), ['1@P', 'failed', '1@P']);  // only post 4 is new
});

test('a continued answer is asked for at once, from exactly its last post', async t => {
  const { asked, settle } = fixture(t, [{ now: T0, events: [] }, { now: at(300), events: [], more: true, after_id: 77 }]);
  await settle(); t.mock.timers.tick(20_000); await settle();
  assert.equal(asked.length, 3);
  assert.match(asked[2], /since=2026-11-01T00%3A05%3A00\.000Z&after_id=77$/);
});

test('a busy window replayed by the overlap is not announced again, however many posts', async t => {
  const ids = Array.from({ length: 6000 }, (_, i) => 1000 + i);
  const { shown, settle } = fixture(t, [{ now: T0, events: [] }, { now: at(20), events: [posts(3, ids)] }, { now: at(40), events: [posts(3, ids)] }]);
  await settle(); t.mock.timers.tick(20_000); await settle();
  assert.deepEqual(shown.map(s => s.text), ['6000@P']);
  t.mock.timers.tick(20_000); await settle();
  assert.equal(shown.length, 1);
});

test('posts this tab uploaded itself are not announced again', async t => {
  const { shown, settle } = fixture(t, [{ now: T0, events: [] }, { now: at(20), events: [posts(7, [40, 41], '')] }]);
  ownPosts([40, 41]);
  await settle(); t.mock.timers.tick(20_000); await settle();
  assert.equal(shown.length, 0);
});

test('a hidden tab stops asking and asks at once when shown again', async t => {
  const { document, asked, settle } = fixture(t, []);
  await settle();
  const before = asked.length;
  document.visibilityState = 'hidden';
  t.mock.timers.tick(20_000); await settle(); t.mock.timers.tick(20_000); await settle();
  assert.equal(asked.length, before);
  document.visibilityState = 'visible';
  document.dispatchEvent(new Event('visibilitychange')); await settle();
  assert.equal(asked.length, before + 1);
});

test('stopping aborts the question in flight and shows nothing from it', async t => {
  const { shown, settle, stop, holdNext } = fixture(t, [{ now: T0, events: [] }, { now: at(20), events: [posts(9, [90])] }]);
  await settle();
  holdNext();
  t.mock.timers.tick(20_000); await settle();
  stop(); await settle();
  assert.equal(shown.length, 0);
});

test('an answer without a usable time does not spin: it waits for the next tick', async t => {
  const { asked, settle } = fixture(t, [{ now: 'not a time', events: [] }]);
  await settle(); await settle();
  assert.equal(asked.length, 1);
  t.mock.timers.tick(20_000); await settle();
  assert.equal(asked.length, 2);
});
