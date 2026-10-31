"""Render actual dashboard template with synthetic service/DB results only."""
import asyncio
import os
import tempfile
from collections import defaultdict
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from api.routers.pools import tagger_dashboard
from api.services import tagger_progress, tagger_situation
class Result:
    def __init__(self, rows=None, scalar=0): self.rows=rows or [];self.value=scalar
    def mappings(self): return self
    def all(self): return self.rows
    def first(self): return None
    def scalar(self): return self.value
    def scalar_one(self): return self.value
    def one(self): return defaultdict(int)
async def execute(sql,*args,**kwargs):
    sql=str(sql)
    if "to_regclass('public.tagger_config')" in sql:return Result(scalar=1)
    if 'SELECT key, value, description' in sql:return Result([dict(key='min_gt_labels',value=10,description='Synthetic threshold',updated_at='2026-09-21',updated_by='fixture')])
    return Result()
observed=dict(tags={'fixture_tag':dict(disposition='hold',reason='downstream_saturated',backlog=2,backlog_max=10)},current=True,min_gt_negative=10,generated_at='2026-09-21T00:00:00Z',paused=False,halt_reason=None,observation_state='current')
with tempfile.TemporaryDirectory() as models, patch.dict(os.environ, {'KURA_MODELS_DIR': models}), patch.object(Path, 'read_text', return_value='{}'), patch.object(tagger_progress,'progress',AsyncMock(return_value=observed)),patch.object(tagger_situation,'sweep_coverage',AsyncMock(return_value=dict(processed=80,total_posts=100,pct=80,remaining=20))),patch.object(tagger_situation,'queue_state',AsyncMock(return_value={'queued':20})),patch.object(tagger_situation,'character_coverage',AsyncMock(return_value=dict(covered=75,total_posts=100,applied_24h={'fixture':3},pixai_hosts=[]))):
    response=asyncio.run(tagger_dashboard(raw=True,db=SimpleNamespace(execute=execute)))
if len(sys.argv) > 1:
    Path(sys.argv[1]).write_bytes(response.body)
else:
    sys.stdout.buffer.write(response.body)
