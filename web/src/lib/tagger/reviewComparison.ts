export type Comparison = { state: string; compared?: number; remaining?: number; counts?: {tp:number;tn:number;fp:number;fn:number}; disagreement_url?: string | null; model_sha256?: string; score_sha256?: string; threshold?: number; population_quality_claim?: boolean };
export function validComparison(comparison:Comparison,total:number):boolean {
 return Boolean(comparison.state === 'compared' && /^[a-f0-9]{64}$/.test(comparison.model_sha256 ?? '') && /^[a-f0-9]{64}$/.test(comparison.score_sha256 ?? '') && typeof comparison.threshold === 'number' && Number.isFinite(comparison.threshold) && comparison.threshold >= 0 && comparison.threshold <= 1 && comparison.population_quality_claim === false && comparison.counts && Object.values(comparison.counts).length === 4 && ['tp','tn','fp','fn'].every(k => Number.isInteger(comparison.counts?.[k as keyof typeof comparison.counts]) && (comparison.counts?.[k as keyof typeof comparison.counts] ?? -1) >= 0) && Object.values(comparison.counts).reduce((a,b)=>a+b,0) === comparison.compared && Number.isInteger(comparison.remaining) && (comparison.remaining ?? -1) >= 0 && (comparison.compared ?? 0) + (comparison.remaining ?? 0) === total);
}
type ReviewPurpose = {
 purpose: string;
 tag?: string;
 review_lane?: string;
 source_provenance?: { origin?: string; kind?: string };
};

export function reviewPurposeKind(review: ReviewPurpose): string {
 const { purpose, tag, review_lane, source_provenance } = review;
 if (purpose === 'age_check' || source_provenance?.kind === 'age_check'
     || source_provenance?.origin === 'age_check'
     || (tag === 'child' && purpose === 'historical_label_recheck')) return 'age_check';
 if (review_lane === 'lora_findings' || source_provenance?.origin === 'lora_findings') return 'lora_findings';
 return purpose;
}

export function purposeLabel(review: ReviewPurpose):string {
 const purpose = reviewPurposeKind(review);
 if (purpose === 'age_check') return '年齢の確認';
 if (purpose === 'lora_findings') return 'LoRA 所見';
 return purpose==='historical_label_recheck'?'古い判断の再確認':purpose==='candidate_evaluation'?'新候補の評価':purpose==='base_comparison'?'新旧モデルの比較':purpose==='boundary_recheck'?'判定境界の確認':purpose==='production_precision'?'予測ありの確認':purpose==='training_support'?'学習用の確認':'固定確認セット';
}
export function disagreementLink(comparison:Comparison|undefined):string|null {
 const link=comparison?.disagreement_url;
 return typeof link==='string' && /^\/\?q=ids%3A\d+(%2C\d+)*$/.test(link) ? link : null;
}
