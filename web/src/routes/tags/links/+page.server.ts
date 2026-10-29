import { redirect } from '@sveltejs/kit';
import type { PageServerLoad } from './$types';

// The artist external-accounts matrix lives on the artists page. Keep this path
// working via a permanent redirect (old links / bookmarks / nav still resolve).
export const load: PageServerLoad = ({ url }) => {
  throw redirect(308, '/artists' + (url.search || ''));
};
