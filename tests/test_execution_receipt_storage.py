import asyncio
import json
from types import SimpleNamespace as N
import pytest
from api.services import execution_receipts as s
from tagger import execution_receipt as c
from tests.test_execution_receipt import body

class DB:
    def __init__(self, heartbeat=None):
        self.rows={};self.heartbeat=heartbeat;self.queries=[]
    async def execute(self, query, params):
        q=str(query);self.queries.append(q)
        if q.startswith('INSERT'):
            self.rows.setdefault(params['ref'],params['wire']);value=None
        elif 'FROM tagger_worker_heartbeat' in q:
            return N(mappings=lambda:N(one_or_none=lambda:self.heartbeat))
        else:
            value=self.rows.get(params['ref'])
            if 'SELECT body' in q and value is not None:value=json.loads(value)
        return N(scalar_one=lambda:value,scalar_one_or_none=lambda:value)

def run(x):return asyncio.run(x)

def test_deduplicated_receipt_and_completion_match_current_worker_heartbeat():
    p=c.make(body());db=DB(dict(host='nodeb',profile='wd14',release_id=14,execution_receipt_sha256=p['receipt_sha256']))
    args=dict(host='nodeb',profile='wd14',release_id=14)
    assert run(s.remember(db,p,**args))==run(s.remember(db,p,**args))==p['receipt_sha256']
    assert len(db.rows)==1
    assert run(s.for_completion(db,receipt_sha256=p['receipt_sha256'],worker_id='w',host='nodeb',release_id=14))==p['receipt_sha256']
    assert any('FOR SHARE' in q for q in db.queries)

@pytest.mark.parametrize('change',[{'host':'other'},{'release_id':15},{'execution_receipt_sha256':None}, {'execution_receipt_sha256':'b'*64}])
def test_restarted_or_cross_context_heartbeat_cannot_label_completion(change):
    p=c.make(body());db=DB(dict(host='nodeb',profile='wd14',release_id=14,execution_receipt_sha256=p['receipt_sha256'])|change)
    with pytest.raises(c.ReceiptRefused):run(s.for_completion(db,receipt_sha256=p['receipt_sha256'],worker_id='w',host='nodeb',release_id=14))

def test_unknown_legacy_does_not_touch_new_schema():
    db=DB()
    assert run(s.for_completion(db,receipt_sha256=None,worker_id=None,host=None,release_id=None)) is None
    assert db.queries==[]

def test_invalid_receipt_or_unregistered_qualification_writes_nothing():
    for field,value in [('host','another'),('qualification_ref','b'*64)]:
        b=body();b[field]=value;p=c.make(b);db=DB()
        with pytest.raises(c.ReceiptRefused):run(s.remember(db,p,host='nodeb',profile='wd14',release_id=14))
        assert db.queries==[]

def test_hash_collision_stored_bytes_are_not_silently_reused():
    p=c.make(body());db=DB();db.rows[p['receipt_sha256']]='{}'
    with pytest.raises(c.ReceiptRefused,match='collision'):run(s.remember(db,p,host='nodeb',profile='wd14',release_id=14))
