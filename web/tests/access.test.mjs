import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import http from 'node:http';
import test from 'node:test';
import ts from 'typescript';

async function moduleSource(name, prelude) {
  const source = await readFile(new URL(`../src/lib/server/${name}.ts`, import.meta.url), 'utf8');
  const js = ts.transpileModule(prelude + source.replace(/^import .*;\r?\n/gm, ''), {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  return import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));
}

test('signed sessions enforce deadline, payload shape and integrity', async () => {
  const m = await moduleSource('session', `import crypto from 'node:crypto'; const env={KURA_SESSION_SECRET:'synthetic-key'};`);
  const signed = m.signSession({ u: 'person', t: 'synthetic-token', e: 'person@example.invalid' });
  assert.equal(m.readSession(signed).u, 'person');
  assert.equal(m.readSession(signed + 'x'), null);
  const crypto = await import('node:crypto');
  function sign(payload) {
    const p = Buffer.from(JSON.stringify(payload)).toString('base64url');
    return p + '.' + crypto.createHmac('sha256', 'synthetic-key').update(p).digest('base64url');
  }
  assert.equal(m.readSession(sign({ u: 'person', t: 'token', e: '', exp: 1 })), null);
  assert.equal(m.readSession(sign({ u: 'person', t: 'token', e: '' })), null);
  assert.equal(m.readSession(sign({ u: {}, t: 'token', e: '', exp: 9999999999 })), null);
});

test('shared access verifies revocation, bounds read cache and refuses cookie CSRF', async t => {
  let status = 200, calls = 0;
  const server = http.createServer((_req, res) => { calls++; res.writeHead(status); res.end(); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); });
  const m = await moduleSource('access', `import crypto from 'node:crypto'; const env={INTERNAL_API_URL:'http://127.0.0.1:${server.address().port}',KURA_API_KEY:'machine-key'};
    const SESSION_COOKIE='session'; const readSession=x=>x==='signed'?{u:'person',t:'token'}:null;
    const authHeaderFromSession=s=>s?'Token synthetic-human':null;`);
  const request = (method, headers={}) => new Request('https://kura.invalid/api/action', { method, headers });
  assert.equal(await m.credential(request('GET'), undefined), null);
  assert.equal(calls, 0);
  const batch = await Promise.all(Array.from({length:10}, () => m.credential(request('GET'), 'signed')));
  assert.ok(batch.every(Boolean));
  assert.equal(calls, 1);
  assert.ok(await m.credential(request('GET'), 'signed'));
  assert.equal(calls, 1);
  assert.equal(await m.credential(request('POST', {origin:'https://evil.invalid'}), 'signed'), null);
  assert.equal(await m.credential(request('POST'), 'signed'), null);
  status = 401;
  assert.equal(await m.credential(request('POST', {origin:'https://kura.invalid'}), 'signed'), null);
  assert.equal(calls, 2); // writes never trust the short read cache
  assert.equal(await m.credential(request('GET'), 'signed'), null); // refusal evicts cached reads
  assert.ok(await m.credential(request('POST', {'x-api-key':'machine-key'}), undefined));
  assert.equal(await m.credential(request('GET', {'x-api-key':'wrong'}), undefined), null);
  status = 503;
  await assert.rejects(m.credential(request('GET'), 'signed'), /authentication_unavailable/);
});

test('a machine key equal to a session or filename secret opens nothing (kura#145)', async () => {
  for (const other of ['KURA_SESSION_SECRET', 'KURA_SECRET', 'KURA_FS_NAME_SALT']) {
    const m = await moduleSource('access', `import crypto from 'node:crypto'; const env={INTERNAL_API_URL:'http://127.0.0.1:9',KURA_API_KEY:'same',${other}:'same'};
      const SESSION_COOKIE='session'; const readSession=()=>null; const authHeaderFromSession=()=>null;`);
    const request = new Request('https://kura.invalid/post/1', { headers: { 'x-api-key': 'same' } });
    assert.equal(await m.credential(request, undefined), null, other);
  }
  const unset = await moduleSource('access', `import crypto from 'node:crypto'; const env={INTERNAL_API_URL:'http://127.0.0.1:9'};
    const SESSION_COOKIE='session'; const readSession=()=>null; const authHeaderFromSession=()=>null;`);
  assert.equal(await unset.credential(new Request('https://kura.invalid/post/1', { headers: { 'x-api-key': '' } }), undefined), null);
});
