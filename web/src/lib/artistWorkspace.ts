import type { TagDetail } from './types';
export type ArtistSource = {
  key: string; id: number | null; kind: string;
  url: string; name: string; active: boolean; state: string | null;
  role?: 'patrol' | 'history'; canonicalUrl?: string;
  intervalHours: number | null; lastAt: string | null; nextAt: string | null;
  error: string | null; cover: string | null; poolId: number | null;
  artistIds: number[]; artistNames: string[]; linkIds: number[]; vanity?: string;
};
export type ArtistSourcePage = { sources: ArtistSource[]; limited: boolean };
export type ArtistIntegration = {
  load: (fetcher: typeof fetch) => Promise<ArtistSourcePage>;
  setActive: (source: ArtistSource, active: boolean) => Promise<unknown>;
  check: (source: ArtistSource) => Promise<unknown>;
  register: (url: string) => Promise<unknown>;
  reference: (url: string) => Promise<{url:string;platform:string}>;
  advancedHref?: string;
  /** where every source is listed when the artist page shows only some */
  listHref?: string;
  receiptHref?: (result: unknown) => string | null;
  /** kinds the core does not know ('search' and 'creator' it does): their label, and whether an active source has never been confirmed */
  describe?: (source: ArtistSource) => { label: string; unverified: boolean } | null;
  /** whether this artist link is already registered with the integration (from link.extensions) */
  registered?: (link: { extensions?: Record<string, Record<string, unknown>> }) => boolean;
};
const adapters = import.meta.glob('/src/lib/artistIntegrations/*.ts', { eager: true }) as
  Record<string, { integration: ArtistIntegration }>;
export const artistIntegration = Object.values(adapters)[0]?.integration;
export function artistEditHref(artist: TagDetail): string {
  return '/tags/edit?' + new URLSearchParams({ q: artist.names[0], cat: 'artist', tagId: String(artist.id), from: `/artists?id=${artist.id}` });
}
export function externalUrl(raw: string): string | null {
  try { const u = new URL(raw); return ['https:', 'http:'].includes(u.protocol) && !u.username && !u.password ? u.href : null; }
  catch { return null; }
}
