import { defineEnvVars } from '@sveltejs/kit/env';

// SvelteKit 3 exposes only the variables declared here ($app/env/private and
// $app/env/public); anything undeclared reads as undefined. Each is read when the
// server starts (not inlined at build time) and each may be unset.
const optional = (value: string | undefined) => value;

export const variables = defineEnvVars({
	INTERNAL_API_URL: { schema: optional, description: 'kura-api base URL the server calls (e.g. http://127.0.0.1:8001)' },
	PUBLIC_API_URL: { public: true, schema: optional, description: 'API base URL for the browser; empty means same origin' },
	KURA_SESSION_SECRET: { schema: optional, description: 'signs the kura_session cookie (ops/install/make_env.py)' },
	KURA_SECRET: { schema: optional, description: 'retiring: the old combined secret, read only as a fallback' },
	KURA_FS_NAME_SALT: { schema: optional, description: 'filename salt, must never sign sessions' },
	KURA_API_KEY: { schema: optional, description: 'machine key the proxy presents to kura-api' },
	KURA_NAV_LINKS: { schema: optional, description: 'JSON list of extra navigation links' },
	KURA_SOURCE_URL: { schema: optional, description: 'where the menu offers the source (AGPL-3.0)' },
	KURA_BACKUP_STATUS_FILE: { schema: optional, description: 'backup status JSON shown on /settings/backup' },
});
