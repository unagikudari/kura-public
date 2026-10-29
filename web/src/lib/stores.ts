import { writable } from 'svelte/store';

/** true の間はファイルドロップアップロードを無効化する */
export const reorderActive = writable(false);

/** Signed-in account (email only — never the token), set by the layout load. */
export interface CurrentUser { email: string | null; name: string; }
export const currentUser = writable<CurrentUser | null>(null);
