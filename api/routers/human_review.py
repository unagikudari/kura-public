"""Canonical explicit user rechecks; never a qualification receipt intake."""
import asyncio,logging,os
from typing import Literal
from uuid import UUID
from fastapi import APIRouter,Depends
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from api.auth import get_current_user
from api.db.models import User
from api.db.session import get_db
from api.errors import KuraError
from api.services import human_review as service, grouped_review
from api.services.review_adjudication import awaiting_resolution, is_adjudication
from api.tagger_core.paired_review import is_paired_review

def canonical():
 if os.environ.get('KURA_DB_ROLE','canonical').lower()!='canonical':
  raise KuraError(503,'human_review_canonical_required',fix='Use the canonical Kura API.')
router=APIRouter(prefix='/api/tagger-review',dependencies=[Depends(canonical)])
class Judgment(BaseModel):
 model_config=ConfigDict(extra='forbid')
 request_id:UUID
 expected_context_sha256:str=Field(pattern='^[0-9a-f]{64}$')
 decision:Literal['present','absent','uncertain']

class GroupJudgment(BaseModel):
 model_config=ConfigDict(extra='forbid')
 request_id:UUID
 expected_context_sha256:str=Field(pattern='^[0-9a-f]{64}$')
 answer:Literal['selected','none','uncertain']
 selected:list[str]=Field(max_length=16)

@router.get('/{ref}')
async def review_set(ref:str,db:AsyncSession=Depends(get_db),user:User=Depends(get_current_user)):
 q=await service.resolve_queue(db,ref)
 await service.bounded_transaction(db)
 ids=await service.reviewable_post_ids(db,q)
 rows=(await db.execute(text('SELECT DISTINCT ON(post_id) post_id,decision,id FROM tagger_human_review_event WHERE queue_sha256=:q AND post_id=ANY(:ids) ORDER BY post_id,id DESC'),{'q':ref,'ids':ids})).mappings().all()
 states={r['post_id']:r['decision'] for r in rows}
 counts={state:sum(s==state for s in states.values()) for state in ('present','absent','uncertain')}
 definition=(await db.execute(text("""
  SELECT t.description, ARRAY(
      SELECT other.name FROM tag_relation r
      JOIN tag_name other ON other.ord=0 AND other.tag_id=
          CASE WHEN r.a_tag_id=t.id THEN r.b_tag_id ELSE r.a_tag_id END
      WHERE r.kind='exclusive' AND t.id IN (r.a_tag_id,r.b_tag_id)
      ORDER BY other.name
  ) AS exclusive_tags
  FROM tag t JOIN tag_name n ON n.tag_id=t.id AND n.ord=0 WHERE n.name=:tag
 """),{'tag':q['tag']})).mappings().one_or_none()
 third=is_adjudication(q)
 answered=counts['present']+counts['absent'] if third else len(states)
 linked=await grouped_review.linked_queues(db,ref)
 if linked:
  incomplete=await grouped_review.incomplete_posts(db,linked,ids)
  states={pid:state for pid,state in states.items() if pid not in incomplete}
  answered=len(ids)-len(incomplete)
 if third and not answered and not await awaiting_resolution(db,q):
  answered=len(ids)
 return dict(counts=counts,tag_description=definition["description"] if definition else None,
             exclusive_tags=definition["exclusive_tags"] if definition else [],
             paired_frame_sha256=q["frame_sha256"] if is_paired_review(q) else None,adjudication=third,queue_sha256=ref,tag=q['tag'],purpose=q['purpose'],source_provenance=q['source_provenance'],post_ids=ids,excluded_count=len(q['post_ids'])-len(ids),decisions=states,remaining=len(ids)-answered,quality_granted=False)


@router.get('/{ref}/posts/{post_id}')
async def review_image(ref:str,post_id:int,db:AsyncSession=Depends(get_db),user:User=Depends(get_current_user)):
 q=await service.resolve_queue(db,ref)
 image=await service.context(db,q,post_id)
 linked=await grouped_review.linked_queues(db,ref)
 group=await grouped_review.describe(db,linked,ref,post_id) if linked else None
 if service.is_blind_evaluation(q):
  image=dict(context=service.blind_image_context(image['context']),
             context_sha256=image['context_sha256'],content_url=image['content_url'])
 if group is not None:
  image['group']=group
 return image

@router.post('/{ref}/posts/{post_id}/group-judgments')
async def judge_group(ref:str,post_id:int,body:GroupJudgment,db:AsyncSession=Depends(get_db),user:User=Depends(get_current_user)):
 try:
  linked=await grouped_review.linked_queues(db,ref)
  if not linked:service.refuse("grouped_review_unconfigured")
  result=await grouped_review.submit(db,linked,ref,post_id,body.model_dump(mode="json"),user)
  await db.commit()
  return result
 except DBAPIError as exc:
  await db.rollback()
  if getattr(exc.orig,'sqlstate',None) in ('55P03','57014','40P01'):
   raise KuraError(503,'human_review_database_busy',fix='Reload the image and retry after the competing operation finishes.') from None
  raise
 except Exception:
  await db.rollback();raise

@router.post('/{ref}/posts/{post_id}/judgments')
async def judge(ref:str,post_id:int,body:Judgment,db:AsyncSession=Depends(get_db),user:User=Depends(get_current_user)):
 try:
  q=await service.resolve_queue(db,ref)
  refill = os.environ.get('KURA_REVIEW_REFILL_ON_COMPLETION') == '1'
  if refill:
   await service.bounded_transaction(db)
   await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                    {'key':'tagger-review-completion:'+ref})
  result=await service.submit(db,q,post_id,body.model_dump(mode='json'),user)
  ids=await service.reviewable_post_ids(db,q)
  decisive = not is_adjudication(q) or result['decision'] != 'uncertain'
  complete = refill and decisive and not result.get('replayed') and (await db.execute(text(
   'SELECT count(DISTINCT post_id) = :total FROM tagger_human_review_event '
   'WHERE queue_sha256=:ref AND post_id=ANY(:ids)'
  ),{'ref':ref,'total':len(ids),'ids':ids})).scalar_one()
  await db.commit()
  if complete:
   from api.tagger_core.review_refill import run
   try:
    await asyncio.wait_for(run(execute=True), timeout=4)
   except Exception as exc:
    logging.getLogger(__name__).warning("Review refill deferred to timer: %s", type(exc).__name__)
  return result
 except DBAPIError as exc:
  await db.rollback()
  if getattr(exc.orig,'sqlstate',None) in ('55P03','57014','40P01'):
   raise KuraError(503,'human_review_database_busy',fix='Reload the image and retry after the competing operation finishes.') from None
  raise
 except Exception:
  await db.rollback();raise
