// Test-only entry for the browser fixtures. SvelteKit 3's adapter-node fixes the
// origin at build time or reads it from request headers, defaulting to https; the
// fixtures serve the built app over plain http on a random port and used to pass
// ORIGIN when starting it, which adapter-node no longer reads. Mark each request
// as http so the derived origin is the one the browser uses, then hand it to the
// built handler.
import http from 'node:http';

process.env.PROTOCOL_HEADER = 'x-kura-fixture-proto';
const { handler } = await import(new URL('../build/handler.js', import.meta.url).href);

http.createServer((req, res) => {
	req.headers['x-kura-fixture-proto'] = 'http';
	handler(req, res);
}).listen(Number(process.env.PORT), process.env.HOST || '127.0.0.1');
