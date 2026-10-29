// Machine access to the web UI: a broker agent key opens the read-only pages.
//
// Every page requires a signed-in human session (hooks.server.ts). That kept
// the UI off the open internet, but it also meant no agent could render a page
// — the only way to measure how the grid behaves under a bulk-edit scroll was
// to ask a person to click (2026-09-05, pool:9607: "人間をセンサーにするな").
//
// The API already recognises fleet members by key (kura#43 item 1) and its
// reads are anonymous; only writes need a user token. So a request carrying
// `X-Api-Key` with a key the broker's /whoami accepts passes the page gate
// WITHOUT a session — the loads run as an anonymous reader, exactly what curl
// against the API gets. No user token is minted, nothing is written, and the
// header cannot elevate to a human account because the session cookie is the
// only thing that carries one.
//
// The broker is asked, not a local copy: the same BROKER_WHOAMI_URL the API
// uses for login (api/config.py), default localhost:8000 = this node's broker
// replica. A rejected key (401) is a miss; an unreachable broker is ALSO a miss
// — the gate fails closed, and the page redirects to /login as before.
import * as env from '$app/env/private';

export const MACHINE_HEADER = 'x-api-key';

export interface MachineIdentity {
  agent_id: string;
  executor: string | null;
  trust_tier: string | null;
}

/** Positive results only, held for a minute so a page's assets and loads do not
 *  each cost a broker round-trip. A miss is never cached: a key that was just
 *  issued must not stay locked out for a minute, and a revoked key is the
 *  broker's call to make, with at most that minute of lag. */
const cache = new Map<string, { until: number; who: MachineIdentity }>();
const TTL_MS = 60_000;

function whoamiUrl(): string {
  return (env.BROKER_WHOAMI_URL ?? 'http://localhost:8000').replace(/\/$/, '') + '/whoami';
}

export async function machineIdentity(key: string | null): Promise<MachineIdentity | null> {
  if (!key) return null;
  const hit = cache.get(key);
  if (hit && hit.until > Date.now()) return hit.who;
  try {
    const res = await fetch(whoamiUrl(), {
      headers: { 'X-API-Key': key },
      signal: AbortSignal.timeout(5000),
    });
    if (!res.ok) return null;
    const j = (await res.json()) as { agent_id?: string; executor?: string; trust_tier?: string };
    if (!j.agent_id) return null;
    const who: MachineIdentity = {
      agent_id: j.agent_id,
      executor: j.executor ?? null,
      trust_tier: j.trust_tier ?? null,
    };
    cache.set(key, { until: Date.now() + TTL_MS, who });
    return who;
  } catch {
    return null; // unreachable broker = no identity = the page gate stays shut
  }
}

/** Test seam: forget every cached identity. */
export function _resetMachineCache(): void {
  cache.clear();
}
