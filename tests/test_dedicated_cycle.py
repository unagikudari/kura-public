import fcntl
import hashlib
import pytest
from tagger import dedicated_cycle as c
from tagger import dedicated_protocol as p


def setup(tmp_path, monkeypatch, *, passed=True):
    doc = p.write_once(tmp_path/'snapshot.json', {'schema':p.SCHEMA,'tag':'test'})
    monkeypatch.setattr(c, 'FrozenImages', lambda *a: type('Images', (), {'doc':{'sha256':'data'}})())
    baseline = tmp_path/'baseline';baseline.write_bytes(b'baseline')
    root=tmp_path/'run';calls=[]
    args={'snapshot':tmp_path/'snapshot.json','media_dir':tmp_path/'data','output':root,
          'baseline':baseline,'baseline_sha256':hashlib.sha256(b'baseline').hexdigest(),
          'baseline_threshold':.85}
    def measure(tag, snapshot, out, **kw):
        calls.append('measure')
        bundle=hashlib.sha256(b'bundle').hexdigest()
        p.write_bytes_once(str(out)+'.bundle.pt',b'bundle')
        candidate=p.write_once(str(out)+'.candidate.json',{'schema':'dedicated-candidate/1',
            'snapshot_sha256':doc['sha256'],'tag':tag,'bundle_sha256':bundle,'threshold':.5})
        return p.write_once(out,{'schema':'vnext-dedicated-measurement/1',
            'snapshot_sha256':doc['sha256'],'tag':tag,'bundle_sha256':bundle,'threshold':.5,
            'candidate_sha256':candidate['sha256']})
    def qualify(snapshot, measurement, media, out, **kw):
        calls.append('qualify');r=p.read_verified(measurement)
        return p.write_once(out,{'schema':'dedicated-serving-qualification/1',
            'snapshot_sha256':doc['sha256'],'measurement_sha256':r['sha256'],
            'bundle_sha256':r['bundle_sha256'],'threshold':.5,
            'baseline_sha256':kw['baseline_sha256'],'baseline_threshold':kw['baseline_threshold'],
            'device':kw['device'],'promotion_eligible':False,
            'parity':{'passed':passed},'quality_floor':{'passed':True},
            'state':'awaiting_production_precision' if passed else 'rejected'})
    monkeypatch.setattr(p,'measure',measure);monkeypatch.setattr(c.q,'qualify',qualify)
    return args,calls


def test_completed_job_reuses_bound_evidence_not_retraining(tmp_path,monkeypatch):
    args,calls=setup(tmp_path,monkeypatch)
    result=c.run(**args)
    assert c.run(**args)==result
    assert calls==['measure','qualify']
    assert result['state']=='awaiting_production_precision'
    assert result['promotion_eligible'] is False


def test_rejected_candidate_is_terminal_not_automatic_retraining(tmp_path,monkeypatch):
    args,calls=setup(tmp_path,monkeypatch,passed=False)
    assert c.run(**args)['state']=='rejected'
    assert c.run(**args)['state']=='rejected'
    assert calls==['measure','qualify']


@pytest.mark.parametrize('stage',['measure','qualify'])
def test_crash_after_complete_output_recovers_without_repeating_stage(tmp_path,monkeypatch,stage):
    args,calls=setup(tmp_path,monkeypatch)
    module=p if stage=='measure' else c.q
    original=getattr(module,stage)
    def crash(*a,**kw):
        original(*a,**kw)
        raise RuntimeError('lost acknowledgement')
    monkeypatch.setattr(module,stage,crash)
    with pytest.raises(RuntimeError):c.run(**args)
    monkeypatch.setattr(module,stage,original)
    assert c.run(**args)['state']=='awaiting_production_precision'
    assert calls==['measure','qualify']


@pytest.mark.parametrize('stage',['measure','qualify'])
def test_partial_stage_is_not_blindly_retried(tmp_path,monkeypatch,stage):
    args,calls=setup(tmp_path,monkeypatch)
    module=p if stage=='measure' else c.q
    def crash(*a,**kw):raise RuntimeError('partial stage')
    monkeypatch.setattr(module,stage,crash)
    with pytest.raises(RuntimeError):c.run(**args)
    with pytest.raises(p.ProtocolError,match='interrupted'):c.run(**args)


def test_changed_binding_and_corrupt_bundle_refuse(tmp_path,monkeypatch):
    args,calls=setup(tmp_path,monkeypatch);c.run(**args)
    with pytest.raises(p.ProtocolError,match='immutable'):
        c.run(**dict(args,baseline_threshold=.84))
    (args['output']/'result.json.bundle.pt').write_bytes(b'corrupt')
    with pytest.raises(p.ProtocolError,match='binding'):c.run(**args)


def test_second_dispatch_cannot_enter(tmp_path,monkeypatch):
    args,calls=setup(tmp_path,monkeypatch);args['output'].mkdir()
    with (args['output']/'cycle.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(p.ProtocolError,match='already running'):c.run(**args)
    assert calls==[]
