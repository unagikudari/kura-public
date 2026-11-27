"""Shared authenticated feedback; author and time come from server, never client."""
import json
import hashlib
import os
from pathlib import Path
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from api.auth import get_current_user
from api.deployment import canonical_url
from api.db.models import User
from api.db.session import get_db
ROOT=Path(__file__).resolve().parents[1]/'dashboard_evaluation'
CRITERIA=json.loads((ROOT/'criteria.json').read_text())
IDS={'general'}|{c['id'] for c in CRITERIA}
REVISION='4588fbd5adfd7014ac228da5777de0890487b349'

REVIEWED_FILES = ('api/routers/pools.py', 'api/tagger_core/dashboard_visual.py', 'api/tagger_core/dashboard_actions.py', 'api/tagger_core/dashboard_experiment.py', 'web/src/routes/tagger-dashboard/+page.svelte', 'web/src/lib/components/EvaluationPane.svelte', 'web/src/lib/components/ReviewActions.svelte', 'web/src/lib/components/ReviewComparison.svelte', 'web/src/lib/components/RuntimeSummary.svelte', 'api/services/dashboard_summary.py', 'web/src/lib/tagger/reviewComparison.ts', 'web/src/lib/components/ReviewQueues.svelte', 'web/src/lib/components/TrainingReadiness.svelte', 'web/src/lib/tagger/reviewRequests.ts', 'web/src/lib/tagger/trainingReadiness.ts', 'api/routers/training_readiness.py', 'api/services/training_readiness.py', 'api/tagger_core/training_eligibility.py')

def current_definition_matches():
 try:
  manifest=json.loads((ROOT/'review-scope.json').read_text())
  expected=manifest['files']
  if manifest.get('reviewed_revision')!=REVISION:return False
  if set(expected)!=set(REVIEWED_FILES):return False
  repo=ROOT.parents[1]
  return all(hashlib.sha256((repo/name).read_bytes()).hexdigest()==expected[name] for name in REVIEWED_FILES)
 except (OSError,ValueError,KeyError,TypeError):return False

router=APIRouter(prefix='/api/tagger-dashboard/evaluation',tags=['dashboard-evaluation'])
class Feedback(BaseModel):
 model_config=ConfigDict(extra='forbid')
 request_id:UUID
 criterion:str
 body:str=Field(min_length=1,max_length=2000)
 page_revision:str
 @field_validator('criterion')
 @classmethod
 def known(cls,v):
  if v not in IDS:raise ValueError('unknown criterion')
  return v
 @field_validator('body')
 @classmethod
 def nonempty(cls,v):
  if not v.strip():raise ValueError('empty comment')
  return v.strip()
 @field_validator('page_revision')
 @classmethod
 def known_revision(cls,v):
  if v!=REVISION:raise ValueError('refresh evaluation page')
  return v
def canonical_comments():
 if os.environ.get("KURA_DB_ROLE", "canonical").strip().lower() != "canonical":
  raise HTTPException(503,"Comments are available on the canonical Kura server only")

@router.get('')
async def evaluation(response:Response,user:User=Depends(get_current_user)):
 response.headers['Cache-Control']='private, no-store'
 reports=[json.loads(p.read_text()) for p in sorted(ROOT.glob('*-review.json'),key=lambda p:(p.name!='self-review.json',p.name))]
 from api.build_info import COMMIT
 return dict(criteria=CRITERIA,reports=reports,current_revision=COMMIT,definition_matches=current_definition_matches(),reviewed_revision=REVISION,scope='2026-11-17 web 4588fbd5 / API 5fafecce。実装者は実画面と操作証跡、Geminiはソース・説明文を独立に検査。Geminiは画面を見ていません。Gemini原回答の採点2件に不一致があり、表示は原回答のassessment列です。操作性の全体判定は未検証。10基準の部分合格・未検証を残し、旧Claude/Gemini評価は履歴に保存。全画面・モデル品質・学習公開の合格ではありません。',viewer_id=user.user_id,canonical_url=canonical_url(os.environ))
@router.get('/comments',dependencies=[Depends(canonical_comments)])
async def comments(response:Response,before:int|None=Query(None,gt=0),db:AsyncSession=Depends(get_db),user:User=Depends(get_current_user)):
 response.headers['Cache-Control']='private, no-store'
 rows=(await db.execute(text('SELECT f.id,f.user_id,u.name AS author,f.criterion,f.body,f.page_revision,f.created_at\n FROM dashboard_feedback f JOIN public."user" u ON u.id=f.user_id\n WHERE f.deleted_at IS NULL AND (CAST(:before AS bigint) IS NULL OR f.id<CAST(:before AS bigint)) ORDER BY f.id DESC LIMIT 51'),{'before':before})).mappings().all()
 return dict(comments=[dict(x) for x in rows[:50]],next_before=rows[49]['id'] if len(rows)>50 else None)
@router.post('/comments',status_code=201,dependencies=[Depends(canonical_comments)])
async def post_comment(payload:Feedback,db:AsyncSession=Depends(get_db),user:User=Depends(get_current_user)):
 params=dict(uid=user.user_id,rid=str(payload.request_id),criterion=payload.criterion,body=payload.body,revision=payload.page_revision)
 row=(await db.execute(text('INSERT INTO dashboard_feedback(user_id,request_id,criterion,body,page_revision)\n VALUES(:uid,CAST(:rid AS uuid),:criterion,:body,:revision)\n ON CONFLICT(user_id,request_id) DO UPDATE SET request_id=EXCLUDED.request_id\n RETURNING id,criterion,body,page_revision,deleted_at'),params)).mappings().one()
 if row['deleted_at'] is not None or (row['criterion'],row['body'],row['page_revision'])!=(payload.criterion,payload.body,payload.page_revision):
  await db.rollback();raise HTTPException(409,'request_id already used for another comment')
 await db.commit()
 return dict(id=row['id'])
@router.delete('/comments/{comment_id}',dependencies=[Depends(canonical_comments)])
async def delete_comment(comment_id:int,db:AsyncSession=Depends(get_db),user:User=Depends(get_current_user)):
 row=(await db.execute(text('UPDATE dashboard_feedback SET deleted_at=coalesce(deleted_at,now()) WHERE id=:id AND user_id=:uid RETURNING id'),dict(id=comment_id,uid=user.user_id))).first()
 if row is None:raise HTTPException(404,'comment not found or not yours')
 await db.commit();return dict(deleted=True)
