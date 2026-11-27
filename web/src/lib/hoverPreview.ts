// How long a grid thumbnail must be hovered before its original shows (owner 2026-11-22: 5 s was long, 3 s;
// changeable from the settings page). 0 = off. Saved in this browser like the other display settings.
import { writable } from 'svelte/store';

export const HOVER_PREVIEW_CHOICES = [0, 1000, 2000, 3000, 5000, 8000] as const;
export const HOVER_PREVIEW_DEFAULT = 3000;
const KEY = 'kura_hover_preview_ms';

function read(): number {
  try {
    const v = Number(localStorage.getItem(KEY));
    return (HOVER_PREVIEW_CHOICES as readonly number[]).includes(v) && localStorage.getItem(KEY) !== null ? v : HOVER_PREVIEW_DEFAULT;
  } catch { return HOVER_PREVIEW_DEFAULT; }
}

export const hoverPreviewMs = writable<number>(typeof localStorage === 'undefined' ? HOVER_PREVIEW_DEFAULT : read());
hoverPreviewMs.subscribe((v) => {
  if (typeof localStorage === 'undefined') return;
  try { localStorage.setItem(KEY, String(v)); } catch { /* private mode: the default still works */ }
});
