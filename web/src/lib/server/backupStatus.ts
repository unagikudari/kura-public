import { readFile, stat } from 'node:fs/promises';
import { homedir } from 'node:os';
import * as env from '$app/env/private';
// Read a small local receipt. Never SSH, hash archives or access recovery envs
// from a page request; the independent status collector owns that work.
export async function backupStatus() {
  const path=env.KURA_BACKUP_STATUS_FILE || `${homedir()}/.local/state/kura-library-backup/status.json`;
  try {
    const info=await stat(path);
    if (!info.isFile() || info.size>32768) throw Error('invalid receipt');
    const value=JSON.parse(await readFile(path,'utf8'));
    const date=(v:unknown)=>typeof v==='string' && Number.isFinite(Date.parse(v));
    const count=(v:unknown)=>Number.isSafeInteger(v) && (v as number)>=0;
    const text=(v:unknown)=>typeof v==='string' && v.length<=256 && !/[\x00-\x1f]/.test(v);
    if (value.format!=='kura-backup-status-v1' || !date(value.checked_at)
      || !['verified','unverified','unavailable','missing'].includes(value.state)
      || !text(value.host) || !text(value.destination)) throw Error('invalid receipt');
    const b=value.backup;
    if (b && (!date(b.created_at) || !date(b.observed_at) || !text(b.snapshot)
      || !count(b.archive_bytes) || !count(b.media_files) || !count(b.tables_verified) || !count(b.constraints_verified)
      || (b.verified_at!==null && !date(b.verified_at)))) throw Error('invalid backup');
    if (value.state==='verified' && !b?.verified_at) throw Error('missing proof');
    const a=value.automation;
    const automation={enabled:a?.enabled===true && date(a.next_at),nextAt:a?.enabled===true && date(a.next_at) ? a.next_at as string : null};
    const j=value.job;
    const job=j && ['running','verified','failed'].includes(j.state) && text(j.snapshot) && date(j.started_at)
      && (j.finished_at===null || date(j.finished_at))
      ? {state:j.state as 'running'|'verified'|'failed',snapshot:j.snapshot as string,startedAt:j.started_at as string,finishedAt:j.finished_at as string|null} : null;
    return { state:value.state as 'verified'|'unverified'|'unavailable'|'missing',checkedAt:value.checked_at as string,
      stale:Date.now()-Date.parse(value.checked_at)>3600000 || Date.parse(value.checked_at)>Date.now()+300000,
      automation,job,host:value.host as string,destination:value.destination as string,
      backup:b ? {snapshot:b.snapshot as string,createdAt:b.created_at as string,observedAt:b.observed_at as string,
        verifiedAt:b.verified_at as string|null,archiveBytes:b.archive_bytes as number,mediaFiles:b.media_files as number,
        tables:b.tables_verified as number,constraints:b.constraints_verified as number} : null };
  } catch { return {automation:{enabled:false,nextAt:null},job:null,state:'unknown' as const,checkedAt:null,stale:true,host:null,destination:null,backup:null}; }
}
