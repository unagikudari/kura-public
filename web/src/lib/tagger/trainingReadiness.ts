export type Target = {queue_sha256:string;frame_sha256:string;tag:string;total:number};
export type Readiness = {schema:string;queue_sha256:string;frame_sha256:string;tag:string;total:number;
  observed_at:string;state:string;counts:Record<string,number>;current_definite:{positive:number;negative:number};
  excluded_from_next_training:number;current_eligibility_counts:Record<string,number>;
  groups:null;groups_state:string;quality_granted:false;admission:string;training_dispatched:false};
const countKeys=['unseen','uncertain','current_definite','gt_not_synced_to_response','current_pin_not_bound_to_response','superseded_response'];
const eligibilityKeys=['uncertain','uncertain_current_pin_order_unresolved','current_definite','gt_not_synced_to_response','current_pin_not_bound_to_response'];
const counts=(value:unknown,keys:string[],total:number):value is Record<string,number> =>
  !!value && typeof value==='object' && !Array.isArray(value) && Object.entries(value).every(([k,v])=>keys.includes(k)&&Number.isSafeInteger(v)&&Number(v)>=0&&Number(v)<=total);
const sum=(value:Record<string,number>)=>Object.values(value).reduce((a,b)=>a+b,0);
export function validateReadiness(value:any,target:Target):Readiness {
  if (!value || value.outcome!=='observed' || !Number.isSafeInteger(value.tag_id) || value.tag_id<1
    || !['sha256','source_provenance_sha256','population_sha256','current_overlay_sha256','responses_overlay_sha256'].every(k=>typeof value[k]==='string'&&/^[a-f0-9]{64}$/.test(value[k]))
    || typeof value.transaction_snapshot!=='string' || !/^[0-9]+:[0-9]+:[0-9,]*$/.test(value.transaction_snapshot)
    || value.schema!=='training-support-readiness/1' || value.scope!=='queue_population_only'
    || value.queue_sha256!==target.queue_sha256 || value.frame_sha256!==target.frame_sha256 || value.tag!==target.tag
    || value.total!==target.total || !Number.isSafeInteger(value.total) || value.total<1 || value.total>500
    || value.model_sha256!==null || value.groups!==null || value.groups_state!=='not_measured'
    || value.quality_granted!==false || value.admission!=='not_granted' || value.training_dispatched!==false
    || typeof value.observed_at!=='string' || !/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value.observed_at) || !Number.isFinite(Date.parse(value.observed_at))
    || !counts(value.counts,countKeys,value.total) || sum(value.counts)!==value.total
    || !counts(value.current_eligibility_counts,eligibilityKeys,value.total) || sum(value.current_eligibility_counts)>value.total
    || !counts(value.current_definite,['positive','negative'],value.total)
    || Object.keys(value.current_definite).length!==2 || sum(value.current_definite)!==(value.counts.current_definite??0)
    || !Number.isSafeInteger(value.excluded_from_next_training) || value.excluded_from_next_training<0
    || value.excluded_from_next_training!==sum(value.current_eligibility_counts)-(value.current_eligibility_counts.current_definite??0)) throw new Error('invalid_readiness');
  const state=value.counts.unseen?'waiting_for_responses':value.counts.uncertain?'blocked_uncertain':value.counts.current_definite===value.total?'responses_bound_not_qualified':'blocked_current_evidence_mismatch';
  if(value.state!==state || value.reason!==state)throw new Error('invalid_readiness');
  return value;
}
export async function readBoundedJSON(response:Response):Promise<unknown> {
  const reader=response.body?.getReader();if(!reader)throw new Error('empty_readiness');
  const parts:Uint8Array[]=[];let total=0;
  try { while(true){const part=await reader.read();if(part.done)break;total+=part.value.length;if(total>65536)throw new Error('readiness_too_large');parts.push(part.value);} }
  finally {await reader.cancel();reader.releaseLock();}
  const all=new Uint8Array(total);let offset=0;for(const part of parts){all.set(part,offset);offset+=part.length;}
  return JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(all));
}
