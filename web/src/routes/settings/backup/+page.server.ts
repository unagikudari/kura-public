import { backupStatus } from '#lib/server/backupStatus.ts';
export const load=async()=>({status:await backupStatus()});
