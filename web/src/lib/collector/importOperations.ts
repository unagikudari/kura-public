import type { ImportFile } from './importFiles';
import type { Messages } from '#lib/i18n/index.ts';
export type ImportItem = { position:number; request_key:string; original_name:string; relative_path:string|null; byte_size:number; sha256:string|null; collection_id:number|null; state:'pending'|'registered'|'rejected'; error_message?:string|null };
export type ImportOperation = { request_key:string; item_count:number; registered_count:number; rejected_count:number; remaining_count:number; state:string; created_at:string };
export type ImportPlan = { request_key:string; collection:number|null; items:{request_key:string; original_name:string; relative_path:string|null; byte_size:number; sha256:string|null}[] };
const MAX_BYTES=64*1024*1024;
export const fileIdentity=(item:ImportFile)=>item.relativePath ?? item.file.name;
export async function digest(file:File):Promise<string|null> {
  if (file.size>MAX_BYTES) return null;
  const value=await crypto.subtle.digest('SHA-256',await file.arrayBuffer());
  return Array.from(new Uint8Array(value),b=>b.toString(16).padStart(2,'0')).join('');
}
export async function readImportItems(key:string,m:Messages):Promise<ImportItem[]> {
  const { collectorApi }=await import('./api');
  const items:ImportItem[]=[];let after=0;
  for (;;) {
    const page=await collectorApi(`imports/${key}/items?after=${after}&limit=200`,undefined,m);
    items.push(...page.items);
    if (items.length>10000) throw new Error(m.collector.operations.planTooLarge);
    if (!page.next_cursor) return items;
    if (page.next_cursor<=after) throw new Error(m.collector.operations.planIncomplete);
    after=page.next_cursor;
  }
}
/** Hash one file at a time; no original bytes or filesystem handles are persisted. */
export async function planImport(files:ImportFile[],collection:number|null,progress:(n:number)=>void):Promise<ImportPlan> {
  const items:ImportPlan['items']=[];
  for (const item of files) {
    progress(items.length+1);
    items.push({request_key:crypto.randomUUID(),original_name:item.file.name,relative_path:item.relativePath ?? null,byte_size:item.file.size,sha256:await digest(item.file)});
  }
  return {request_key:crypto.randomUUID(),collection,items};
}
export async function matchImportFiles(manifest:ImportItem[],files:ImportFile[],progress:(n:number)=>void,m:Messages) {
  const indexed=new Map(manifest.map(item=>[item.relative_path ?? item.original_name,item]));
  const matched:{input:ImportFile;item:ImportItem}[]=[];let ignored=0;let checked=0;
  for (const input of files) {
    const item=indexed.get(fileIdentity(input));
    if (!item) { ignored++;continue; }
    progress(++checked);
    if (input.file.size!==item.byte_size || await digest(input.file)!==item.sha256)
      throw new Error(m.collector.operations.mismatch(fileIdentity(input)));
    matched.push({input,item});
  }
  const keys=new Set(matched.map(m=>m.item.request_key));
  return {matched,ignored,missing:manifest.filter(item=>item.state==='pending' && !keys.has(item.request_key)).length};
}
