export type ExplicitRequest = {schema:string;queue_sha256:string;frame_sha256:string;reason:string;evidence_sha256:string;requested_at:string};
export type Queue = { review_lane?: "lifecycle" | "lora_findings"; source_provenance?: { origin?: string; kind?: string }; reselectable?:boolean; schema?:string; judged?:Record<string,number>; review_state?:string; frame_sha256?:string; request?:ExplicitRequest|null; request_unavailable?:boolean; comparison?: {state:string; compared?:number; remaining?:number; counts?:{tp:number;tn:number;fp:number;fn:number}; disagreement_url?:string|null; population_quality_claim?:boolean; model_sha256?:string; score_sha256?:string; threshold?:number}; tag: string; purpose: string; total: number; remaining: number; queue_sha256: string; browse_url: string; unjudged_url: string };

export const historical=(q:Queue)=>q.purpose==='historical_label_recheck';
export const explicitReview=(q:Queue)=>(q.schema==='human-review-set/2'||q.schema==='human-review-set/3')||historical(q);
export const explicit=(q:Queue)=>validRequest(q) && q.request?.reason==='class_group_support';
export const requested=(q:Queue)=>(explicit(q) || ['boundary_recheck','candidate_evaluation','historical_label_recheck'].includes(q.purpose)) && q.remaining>0;
export function validRequest(q:Queue):boolean {
    const r=q.request;
    if (r==null) return true;
    return typeof r==='object' && Object.keys(r).sort().join(',')==='evidence_sha256,frame_sha256,queue_sha256,reason,requested_at,schema' && q.purpose==='training_support' && r.schema==='review-request/1' && r.reason==='class_group_support' && r.queue_sha256===q.queue_sha256 && r.frame_sha256===q.frame_sha256 && [r.frame_sha256,r.evidence_sha256].every(h=>typeof h==='string' && /^[a-f0-9]{64}$/.test(h)) && typeof r.requested_at==='string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(r.requested_at) && Number.isFinite(Date.parse(r.requested_at));
  }

export function validateSnapshot(data:any): void {
      if (data.state !== 'available' || !Array.isArray(data.queues) || data.queues.some((q: Queue) => !q || typeof q.tag !== 'string' || typeof q.purpose !== 'string' || typeof q.queue_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(q.queue_sha256) || ![q.browse_url,q.unjudged_url].every(u => typeof u === 'string' && (/^\/\?q=/.test(u) || /^\/tagger-review\/[a-f0-9]{64}$/.test(u))) || !Number.isInteger(q.total) || q.total <= 0 || !Number.isInteger(q.remaining) || q.remaining < 0 || q.remaining > q.total) || new Set(data.queues.map((q: Queue) => q.queue_sha256)).size !== data.queues.length) throw new Error('unavailable');
      if (data.unavailable_queues !== undefined && (!Array.isArray(data.unavailable_queues) || data.unavailable_queues.some((q:any)=>!q||typeof q.tag!=='string'||!Number.isInteger(q.total)||q.total<1||!['human_review_canonical_required','human_review_storage_unavailable'].includes(q.reason)))) throw new Error('unavailable');
      if (data.queues.some((q:Queue)=>!validRequest(q) || (q.request_unavailable!==undefined && typeof q.request_unavailable!=='boolean') || (q.request_unavailable && q.request))) throw new Error('unavailable');
      if (data.requests_state!==undefined && !['available','not_configured','unavailable'].includes(data.requests_state)) throw new Error('unavailable');
      const metadataUnavailable=data.requests_state==='unavailable';
      if (metadataUnavailable !== (data.request_unavailable?.reason==='request_metadata_unavailable') || data.queues.some((q:Queue)=>(q.request_unavailable && !metadataUnavailable) || (metadataUnavailable && q.purpose==='training_support' && !q.request_unavailable) || (q.request && data.requests_state!=='available'))) throw new Error('unavailable');
}

// Legacy pin queues omit zero-valued keys; explicit queues also count uncertainty.
export function validJudgments(q:Queue): boolean {
  const counts=q.judged;
  const states=explicitReview(q)?['present','absent','uncertain']:['present','absent'];
  return !!counts && typeof counts==='object' && !Array.isArray(counts)
    && Object.keys(counts).every(k=>states.includes(k))
    && Object.values(counts).every(n=>Number.isInteger(n) && n>=0 && n<=q.total)
    && Object.values(counts).reduce((sum,n)=>sum+n,0)===q.total-q.remaining;
}
