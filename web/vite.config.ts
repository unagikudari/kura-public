import { existsSync, readFileSync } from 'node:fs';
import { relative, sep } from 'node:path';
import adapter from '@sveltejs/adapter-node';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

// adapter-node 6 no longer reads ORIGIN when the server starts: the origin is fixed
// when the app is built. Take it from the environment, else from web/.env.server
// (ops/install/make_env.py writes it before the README's build step). Changing the
// origin therefore means building again.
function origin(): string | undefined {
	if (process.env.ORIGIN) return process.env.ORIGIN;
	const file = new URL('./.env.server', import.meta.url);
	if (!existsSync(file)) return undefined;
	const line = readFileSync(file, 'utf8').split('\n').find((l) => l.startsWith('ORIGIN='));
	return line?.slice('ORIGIN='.length).trim().replace(/^["']|["']$/g, '') || undefined;
}

export default defineConfig({
	plugins: [
		sveltekit({
			adapter: adapter(),
			compilerOptions: {
				// defaults to rune mode for the project, except for `node_modules`. Can be removed in svelte 6.
				runes: ({ filename }) => {
					const relativePath = relative(import.meta.dirname, filename);
					const pathSegments = relativePath.toLowerCase().split(sep);
					const isExternalLibrary = pathSegments.includes('node_modules');

					return isExternalLibrary ? undefined : true;
				}
			},
			// The shared ingress checks cookie Origin itself. Native workers send
			// multipart with explicit credentials and no browser Origin header, which
			// SvelteKit 3 refuses unless every origin is trusted ('*' turns its check off,
			// as checkOrigin: false did in 2.x).
			csrf: { trustedOrigins: ['*'] },
			paths: { origin: origin() },
			// 長寿命タブの stale バンドル対策 (2026-07-19): デプロイ検出。SPA 遷移では
			// バンドルを再取得しないため、開きっぱなしのタブは旧 UI のまま固定される —
			// 「直したのに直ってない」の構造原因。$updated + beforeNavigate (+layout) で
			// 新ビルド検出時に次のナビゲーションでフルリロードする。
			version: { pollInterval: 60_000 }
		})
	]
});
