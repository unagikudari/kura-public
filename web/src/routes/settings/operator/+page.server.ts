// Read through the same-origin proxy so the session's credential is used server-side.
export const load = async ({ fetch }) => {
  const res = await fetch('/m/settings/operator');
  if (res.status === 401 || res.status === 403) return { state: 'forbidden' as const, data: null };
  if (!res.ok) return { state: 'failed' as const, data: null };
  return { state: 'ok' as const, data: await res.json() };
};
