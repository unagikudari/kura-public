import type { IconName } from '#lib/components/Icon.svelte';
// Optional UI contributions exist only in a composed extension build.
import type { PoolSummary } from './types.ts';

export interface PoolBadge { text: string; title: string; className: string }
/** What an extension adds to a pool card and the pool's source list, read from the
 *  data it attached to the pool (`pool.extensions[<its id>]`). */
export interface PoolDecor {
  /** The page count the pool is expected to reach, when the extension knows it. */
  expected?(pool: PoolSummary): number | null | undefined;
  badges?(pool: PoolSummary): PoolBadge[];
  /** A short note beside one source URL of the pool. */
  sourceNote?(extensions: PoolSummary['extensions'], url: string): string | null | undefined;
}

/** A page an extension adds: the header link, the Ops menu entry, the loading notice. */
// icon: shown in the header instead of the label (which stays the link's name and tooltip).
export interface ExtensionNav { href: string; label: string; icon?: IconName; menuLabel?: string; loadingLabel?: string }

const modules = import.meta.glob('/src/lib/extensions/*.ts', { eager: true }) as
  Record<string, { id?: string; pool?: PoolDecor; nav?: ExtensionNav }>;
export const hasLoraUI = Object.values(modules).some(module => module.id === 'lora');
export const extensionNav: ExtensionNav[] = Object.values(modules).flatMap(module => module.nav ? [module.nav] : []);
const decors = Object.values(modules).flatMap(module => module.pool ? [module.pool] : []);

export function poolExpected(pool: PoolSummary): number | null {
  for (const decor of decors) {
    const expected = decor.expected?.(pool);
    if (expected != null) return expected;
  }
  return null;
}

export function poolBadges(pool: PoolSummary): PoolBadge[] {
  return decors.flatMap(decor => decor.badges?.(pool) ?? []);
}

export function poolSourceNote(extensions: PoolSummary['extensions'], url: string): string | null {
  for (const decor of decors) {
    const note = decor.sourceNote?.(extensions, url);
    if (note) return note;
  }
  return null;
}
