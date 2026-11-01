/** Redraw an element when the page comes back from the background.
 *
 * On 2026-11-01 the owner's iPad (Chrome, i.e. WebKit) showed kura's sticky header black
 * after switching apps, while the rest of the page was fine, until a reload. WebKit can
 * drop a composited layer's contents while the app is in the background. Changing opacity
 * for two frames asks it to composite that layer again; WebKit may do so without
 * repainting, so whether this cures it is for the iPad to show. */
import type { Action } from 'svelte/action';

export const repaintOnResume: Action<HTMLElement> = node => {
  let frame = 0;
  let restore: string | null = null;
  const settle = () => { if (restore !== null) { node.style.opacity = restore; restore = null; } };
  const nudge = () => {
    if (document.visibilityState !== 'visible' || restore !== null) return;
    restore = node.style.opacity;
    node.style.opacity = '0.999';
    frame = requestAnimationFrame(() => { frame = requestAnimationFrame(() => { frame = 0; settle(); }); });
  };
  document.addEventListener('visibilitychange', nudge);
  addEventListener('pageshow', nudge);
  return {
    destroy() {
      document.removeEventListener('visibilitychange', nudge);
      removeEventListener('pageshow', nudge);
      if (frame) cancelAnimationFrame(frame);
      settle();
    }
  };
};
