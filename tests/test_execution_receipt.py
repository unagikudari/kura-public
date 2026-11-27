import copy
import pytest
from tagger import execution_receipt as r


def body():
    return dict(schema='kura.character-execution/1',host='nodeb',profile='wd14',release_id=14,
        component_source='cl_tagger_v2='+'a'*64,providers=['ROCMExecutionProvider','CPUExecutionProvider'],
        ort_version='1.22.2',pillow_version='12.0.0',numpy_version='2.2.0',intra_op_threads=2,
        inter_op_threads=1,execution_mode='sequential',device_id=0,gpu_mem_limit=3*1024**3,
        arena_extend_strategy='kSameAsRequested',qualification_ref=None)


def test_canonical_order_dedup_and_snapshot_immutability():
    b=body();p=r.make(b)
    assert p==r.make(dict(reversed(list(b.items()))))
    b['providers'].clear()
    assert p['body']['providers']==['ROCMExecutionProvider','CPUExecutionProvider']
    assert len(r.canonical(p['body'])) <= r.MAX_BYTES


@pytest.mark.parametrize('key,value', [
    ('release_id',True),('intra_op_threads',False),('intra_op_threads',17),('inter_op_threads',2),
    ('gpu_mem_limit',3.0),('gpu_mem_limit',1),('device_id',True),('device_id',-1),
    ('providers',['ROCMExecutionProvider','ROCMExecutionProvider']),('providers',['CPUExecutionProvider','ROCMExecutionProvider']),
    ('providers',['unknown']),('providers',[]),('ort_version','private message\n'),
    ('host','http://private'),('qualification_ref','/private/path'),('component_source','anything'),
    ('execution_mode','parallel'),('profile','other'),('schema','other'),
])
def test_malformed_facts_refused(key,value):
    b=body();b[key]=value
    with pytest.raises(r.ReceiptRefused):r.make(b)


def test_no_free_form_fields_or_timestamps():
    for key in ('started_at','token','source','message'):
        b=body();b[key]='forbidden'
        with pytest.raises(r.ReceiptRefused):r.make(b)


def test_content_tampering_and_cross_worker_context_refused():
    p=r.make(body());p['body']['ort_version']='1.23.0'
    with pytest.raises(r.ReceiptRefused,match='digest'):r.verify(p,host='nodeb',profile='wd14',release_id=14)
    for args in [dict(host='nodec',profile='wd14',release_id=14),dict(host='nodeb',profile='full',release_id=14),dict(host='nodeb',profile='wd14',release_id=15),dict(host='nodeb',profile='wd14',release_id=True)]:
        with pytest.raises(r.ReceiptRefused,match='context'):r.verify(r.make(body()),**args)


def test_completion_requires_same_current_heartbeat():
    p=r.make(body())
    assert r.bind_completion(p,host='nodeb',profile='wd14',release_id=14,heartbeat_receipt=p['receipt_sha256'])==p
    for other in (None,'b'*64):
        with pytest.raises(r.ReceiptRefused,match='heartbeat_mismatch'):
            r.bind_completion(p,host='nodeb',profile='wd14',release_id=14,heartbeat_receipt=other)


def test_cpu_is_explicit_unknown_is_not_cpu():
    b=body();b.update(providers=['CPUExecutionProvider'],device_id=None,gpu_mem_limit=None,arena_extend_strategy=None)
    assert r.make(b)
    with pytest.raises(r.ReceiptRefused):r.make(None)
    b['gpu_mem_limit']=3*1024**3
    with pytest.raises(r.ReceiptRefused):r.make(b)
