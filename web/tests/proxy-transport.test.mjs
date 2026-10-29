import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import http from 'node:http';
import test from 'node:test';
import ts from 'typescript';

// Execute the production route. Only SvelteKit's environment and session imports
// are replaced; upstream fetch, request streams and socket failures are real.
const source = await readFile(new URL('../src/lib/server/proxy.ts', import.meta.url), 'utf8');
async function fixture(t, respond, envExtra = {}, identity = { authorization: 'Token synthetic-test', machine: false }) {
  const requests = [];
  const headersSeen = [];
  const server = http.createServer(async (req, res) => {
    let bytes = 0;
    for await (const chunk of req) bytes += chunk.length;
    requests.push({ method: req.method, bytes });
    headersSeen.push(req.headers);
    respond(req, res, requests.length);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  });
  const prelude = `const env={INTERNAL_API_URL:'http://127.0.0.1:${server.address().port}'${Object.entries(envExtra).map(([k, v]) => `,${k}:${JSON.stringify(v)}`).join('')}};
    const SESSION_COOKIE='session'; const credential=async()=>(${JSON.stringify(identity)});
    import { ja as pagesJa } from ${JSON.stringify(new URL('../src/lib/i18n/parts/pages.ts', import.meta.url).href)};
    const UI_LOCALE_COOKIE='kura_ui_locale'; const resolveUiLocale=()=>'ja'; const messagesFor=()=>({ pages: pagesJa });`;
  const javascript = ts.transpileModule(prelude + source.replace(/^import .*;\r?\n/gm, ''), {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const route = await import('data:text/javascript;base64,' + Buffer.from(javascript).toString('base64'));
  return {
    requests,
    headersSeen,
    async call(method, body, declaredLength = true) {
      const url = new URL('http://synthetic.invalid/m/action');
      const request = new Request(url, {
        method, body,
        headers: body !== undefined && declaredLength ? { 'content-length': String(Buffer.byteLength(body)) } : {},
      });
      // Mutation bodies must remain streamed, regardless of size or length header.
      request.arrayBuffer = () => { throw new Error('proxy buffered a mutation'); };
      return (route[method] ?? route.GET)({ params: { path: 'action' }, request, url, cookies: { get: () => 'synthetic' } });
    },
  };
}

for (const method of ['POST', 'PUT', 'PATCH', 'DELETE']) {
  for (const body of [undefined, '{}']) {
    test(`${method} ${body === undefined ? 'without' : 'with'} body: lost response never repeats the effect`, { timeout: 5000 }, async t => {
      const f = await fixture(t, (req, res, count) => {
        if (count === 1) req.socket.destroy(); // the effect already happened
        else res.end('duplicate effect');
      });
      const response = await f.call(method, body);
      assert.equal(response.status, 502);
      const { detail } = await response.json();
      assert.equal(detail.error, 'upstream_result_unknown');
      assert.equal(detail.context.retried, false);
      assert.match(detail.fix, /処理状況を確認/);
      assert.equal(f.requests.length, 1);
    });
  }
}

for (const method of ['GET', 'HEAD']) {
  test(`${method}: retries one lost response`, { timeout: 5000 }, async t => {
    const f = await fixture(t, (req, res, count) => {
      if (count === 1) req.socket.destroy();
      else { res.setHeader('etag', 'synthetic'); res.end(method === 'HEAD' ? undefined : 'read result'); }
    });
    const response = await f.call(method);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('etag'), 'synthetic');
    assert.equal(await response.text(), method === 'HEAD' ? '' : 'read result');
    assert.equal(f.requests.length, 2);
  });
}

test('GET stops after two connection failures', { timeout: 5000 }, async t => {
  const f = await fixture(t, req => req.socket.destroy());
  const response = await f.call('GET');
  assert.equal(response.status, 502);
  const { detail } = await response.json();
  assert.equal(detail.error, 'upstream_response_unavailable');
  assert.equal(detail.context.retried, true);
  assert.equal(f.requests.length, 2);
});

test('an upstream HTTP error is preserved without retry', { timeout: 5000 }, async t => {
  const f = await fixture(t, (_req, res) => {
    res.writeHead(403, { 'content-type': 'application/json' });
    res.end('{"detail":"refused"}');
  });
  const response = await f.call('POST', '{}');
  assert.equal(response.status, 403);
  assert.deepEqual(await response.json(), { detail: 'refused' });
  assert.equal(f.requests.length, 1);
});

for (const declaredLength of [true, false]) {
  test(`large upload stays streamed, declared length=${declaredLength}`, { timeout: 5000 }, async t => {
    const f = await fixture(t, (_req, res) => res.end('stored'));
    const body = 'x'.repeat(2 * 1024 * 1024);
    const response = await f.call('POST', body, declaredLength);
    assert.equal(response.status, 200);
    assert.equal(await response.text(), 'stored');
    assert.deepEqual(f.requests, [{ method: 'POST', bytes: body.length }]);
  });
}


// ── the auth challenge that was reported as an unknown outcome ────────────────
// Measured on primary 2026-09-16: kura-api gates the machine-gated extension routes behind
// X-Api-Key (api/main.py _MACHINE_GATE) in enforce mode, and this proxy did not forward
// one, so every UI write to those routes got a 401. undici re-sends on an auth challenge, a streamed
// body cannot be replayed, the throw was swallowed by a bare `catch {}`, and the user
// was told the write "may have been submitted" — for a request that had been refused
// and never ran. Nothing had been submitted.

test('an upstream 401 is reported as refusal, not as an unknown outcome', { timeout: 5000 }, async t => {
  const f = await fixture(t, (_req, res) => {
    res.writeHead(401, { 'content-type': 'application/json' });
    res.end('{"detail":"nope"}');
  });
  const response = await f.call('POST', '{}');
  assert.equal(response.status, 401);
  const { detail } = await response.json();
  assert.equal(detail.error, 'upstream_auth_rejected');
  assert.match(detail.fix, /送信は行われていません/);
  // Reported, never retried: the body stayed streamed, so it was sent exactly once.
  assert.equal(f.requests.length, 1);
});

test('the machine key is attached server-side, never asked of the browser', { timeout: 5000 }, async t => {
  const f = await fixture(t, (_req, res) => res.end('ok'), { KURA_API_KEY: 'synthetic-key' });
  const response = await f.call('POST', '{}');
  assert.equal(response.status, 200);
  assert.equal(f.headersSeen[0]['x-api-key'], 'synthetic-key');
});

test('no key configured attaches no header, so the gate still speaks for itself', { timeout: 5000 }, async t => {
  const f = await fixture(t, (_req, res) => res.end('ok'));
  await f.call('POST', '{}');
  assert.equal(f.headersSeen[0]['x-api-key'], undefined);
});

for (const method of ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE']) {
  test(`anonymous ${method} never reaches upstream or borrows its key`, async t => {
    const f = await fixture(t, (_req, res) => res.end('must not run'), { KURA_API_KEY: 'synthetic-key' }, null);
    const response = await f.call(method, method === 'GET' || method === 'HEAD' ? undefined : '{}');
    assert.equal(response.status, 401);
    assert.equal(f.requests.length, 0);
  });
}

test('redirects are returned without forwarding credentials to the next server', async t => {
  const f = await fixture(t, (_req, res) => { res.writeHead(307, { location: 'https://outside.invalid/file' }); res.end(); }, { KURA_API_KEY: 'synthetic-key' });
  const response = await f.call('GET');
  assert.equal(response.status, 307);
  assert.equal(response.headers.get('location'), 'https://outside.invalid/file');
  assert.equal(f.requests.length, 1);
});
