import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import http from 'node:http';
import test from 'node:test';
import ts from 'typescript';

const routeSource = await readFile(new URL('../src/routes/login/+page.server.ts', import.meta.url), 'utf8');
const sessionSource = await readFile(new URL('../src/lib/server/session.ts', import.meta.url), 'utf8');
const errors = await readFile(new URL('../src/lib/apiError.ts', import.meta.url), 'utf8');
const rawKey = '1'.repeat(64);
const compile = source => 'data:text/javascript;base64,' + Buffer.from(ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText).toString('base64');

async function fixture(t, status, body, origin='http://test.invalid') {
  let seen;
  const upstream = http.createServer(async (req, res) => {
    let data=''; for await (const chunk of req) data += chunk;
    seen={url:req.url,method:req.method,body:JSON.parse(data)};
    res.writeHead(status, {'content-type':'application/json'}); res.end(JSON.stringify(body));
  });
  await new Promise(resolve=>upstream.listen(0,'127.0.0.1',resolve));
  t.after(async()=>{ upstream.closeAllConnections(); await new Promise(resolve=>upstream.close(resolve)); });
  const env={INTERNAL_API_URL:`http://127.0.0.1:${upstream.address().port}`,KURA_SESSION_SECRET:'test-secret-only-not-production'};
  const session=await import(compile('const env='+JSON.stringify(env)+';\n'+sessionSource.replace(/^import .*private.*;\r?\n/gm,'')));
  const sessionUrl=compile('const env='+JSON.stringify(env)+';\n'+sessionSource.replace(/^import .*private.*;\r?\n/gm,''));
  const prelude=`const env=${JSON.stringify(env)};
    const fail=(status,data)=>({status,data});
    const redirect=(status,location)=>({status,location});
    import {SESSION_COOKIE,signSession} from ${JSON.stringify(sessionUrl)};`;
  const route=await import(compile(prelude+errors+routeSource.replace(/^import .*;\r?\n/gm,'')));
  const cookies=[];
  const headers=origin?{origin}:{};
  return {session,cookies,seen:()=>seen,call:()=>route.actions.default({
    request:new Request('http://test.invalid/login',{method:'POST',headers,body:new URLSearchParams({email:'person@example.invalid',key:rawKey})}),
    url:new URL('http://test.invalid/login'),
    cookies:{set:(...args)=>cookies.push(args)},
  })};
}

test('actual SSR action signs only the returned local session and redirects',async t=>{
  const f=await fixture(t,200,{username:'person',token:'synthetic-session-token',email:'person@example.invalid'});
  await assert.rejects(f.call(),e=>e.status===303&&e.location==='/');
  assert.deepEqual(f.seen(),{url:'/api/auth/login',method:'POST',body:{email:'person@example.invalid',key:rawKey}});
  const [name,value,options]=f.cookies[0];
  assert.equal(name,'kura_session');
  assert.equal(options.httpOnly,true); assert.equal(options.secure,true); assert.equal(options.sameSite,'lax');
  const {exp,...rest}=f.session.readSession(value);
  assert.deepEqual(rest,{u:'person',t:'synthetic-session-token',e:'person@example.invalid'});
  assert.ok(exp>Date.now()/1000,'the session carries an expiry in the future');
  assert.ok(!JSON.stringify(f.session.readSession(value)).includes(rawKey));
});
for(const status of [401,403,503]) test(`SSR preserves ${status} without cookie or echoed key`,async t=>{
  const f=await fixture(t,status,{detail:'refused'});
  const result=await f.call(); assert.equal(result.status,status); assert.equal(f.cookies.length,0);
  assert.ok(!JSON.stringify(result).includes(rawKey));
});
for(const origin of ['http://other.invalid',null]) test(`SSR refuses a sign-in posted from ${origin??'no'} origin before asking the API`,async t=>{
  const f=await fixture(t,200,{username:'person',token:'synthetic-session-token'},origin);
  const result=await f.call(); assert.equal(result.status,403); assert.equal(f.cookies.length,0);
  assert.equal(f.seen(),undefined);
});
