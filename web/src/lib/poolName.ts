/** A pool name can carry an account id of the site it came from. The id is shown as a link to the
 *  account's page, never as bare digits: iOS reads a run of digits as a phone number and a tap
 *  dials it (2026-11-02 owner). Which sites do this and how is site knowledge (kura#66 G6): the
 *  rules come from the site adapter through #lib/site.ts, and without one a name is shown as is. */

/** Where one site's account id sits in a pool name, and the account page it names. */
export type PoolNameAccountRule = {
  label: string;
  find(name: string): { start: number; end: number; url: string } | null;
};

export type PoolNameParts = { before: string; account: { label: string; url: string } | null; after: string };

export function splitPoolName(name: string, rules: readonly PoolNameAccountRule[]): PoolNameParts {
  for (const rule of rules) {
    const found = rule.find(name);
    if (found) return {
      before: name.slice(0, found.start).replace(/[\s_]+$/, ''),
      account: { label: rule.label, url: found.url },
      after: name.slice(found.end).replace(/^[\s_]+/, ''),
    };
  }
  return { before: name, account: null, after: '' };
}

/** "[artist" + "] title" closes the bracket with no space between. */
export function joinParts(before: string, after: string): string {
  if (!before || !after) return before || after;
  return /^[)\]）]/.test(after) ? before + after : `${before} ${after}`;
}

/** The name without the account id, for places that cannot hold a link of their own (inside a
 *  link, a tooltip). A name that was only the id reads as the site's label. */
export function poolDisplayName(name: string, rules: readonly PoolNameAccountRule[]): string {
  const { before, account, after } = splitPoolName(name, rules);
  return joinParts(before, after) || account?.label || name;
}
