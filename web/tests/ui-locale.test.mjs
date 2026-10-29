import assert from 'node:assert/strict';
import test from 'node:test';
import { messagesFor, resolveUiLocale } from '../src/lib/i18n/index.ts';

test('the person\'s choice wins, then the install default, then the browser, then English', () => {
  delete process.env.KURA_UI_LOCALE;
  assert.equal(resolveUiLocale('ja', 'en-US,en'), 'ja');
  assert.equal(resolveUiLocale(undefined, 'fr-FR,ja;q=0.8,en;q=0.5'), 'ja');
  assert.equal(resolveUiLocale(undefined, 'ja;q=0.3,en-GB;q=0.9'), 'en');
  assert.equal(resolveUiLocale(undefined, 'fr,de'), 'en');
  assert.equal(resolveUiLocale('xx', null), 'en');
  process.env.KURA_UI_LOCALE = 'ja';
  try {
    assert.equal(resolveUiLocale(undefined, 'en-US'), 'ja');
    assert.equal(resolveUiLocale('en', 'ja'), 'en');
  } finally { delete process.env.KURA_UI_LOCALE; }
});

test('every language has every message, and the same kind of message', () => {
  const walk = (a, b, path) => {
    assert.deepEqual(Object.keys(b).sort(), Object.keys(a).sort(), path);
    for (const k of Object.keys(a)) {
      assert.equal(typeof b[k], typeof a[k], `${path}.${k}`);
      if (typeof a[k] === 'object') walk(a[k], b[k], `${path}.${k}`);
    }
  };
  walk(messagesFor('en'), messagesFor('ja'), 'm');
});
