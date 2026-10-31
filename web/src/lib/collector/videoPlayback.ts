// One decoder at a time in the collector. Calls happen only on client interaction.
let current: (() => void) | undefined;
export function claimPlayback(stop: () => void): () => void {
  const previous = current;
  current = stop;
  previous?.();
  return () => { if (current === stop) current = undefined; };
}
