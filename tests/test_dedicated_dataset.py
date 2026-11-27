import hashlib
from pathlib import Path
import pytest
from tagger import dedicated_dataset as ds, dedicated_protocol as p


def world(tmp_path,monkeypatch):
    monkeypatch.setattr(p,'validate_snapshot',lambda doc:None)
    body=b'fixed display bytes';sha=hashlib.sha256(body).hexdigest()
    doc={'sha256':'snapshot','split':{'train':[1],'heldout':[]},'media':[{'id':1,'blob_sha256':sha}]}
    p.write_once(tmp_path/'dataset.json',{'schema':'dedicated-dataset/1','snapshot_sha256':'snapshot','images':{'1':sha},'bytes':len(body)})
    (tmp_path/'1.bin').write_bytes(body)
    return doc,body


def test_local_inputs_are_bound_and_checked_each_read(tmp_path,monkeypatch):
    doc,body=world(tmp_path,monkeypatch);reader=ds.FrozenImages(tmp_path,doc)
    assert reader([1])==[(1,body)]
    (tmp_path/'1.bin').write_bytes(b'changed')
    with pytest.raises(p.ProtocolError,match='bytes changed'):reader([1])
    with pytest.raises(p.ProtocolError,match='outside'):reader(['../1'])


def test_other_snapshot_and_symlink_refused(tmp_path,monkeypatch):
    doc,body=world(tmp_path,monkeypatch)
    doc['sha256']='other'
    with pytest.raises(p.ProtocolError,match='binding'):ds.FrozenImages(tmp_path,doc)
    doc['sha256']='snapshot';reader=ds.FrozenImages(tmp_path,doc)
    (tmp_path/'1.bin').unlink();(tmp_path/'elsewhere').write_bytes(body);(tmp_path/'1.bin').symlink_to(tmp_path/'elsewhere')
    with pytest.raises(p.ProtocolError,match='symlink'):reader([1])


def test_measure_offline_emits_worker_bundle_before_holdout(tmp_path,monkeypatch):
    import io,json,torch,timm
    from PIL import Image
    from tagger import feature_contract as fc, content_head_model as cm, vnext_arms as arms
    class Backbone(torch.nn.Module):
        num_features=3
        def __init__(self):
            super().__init__();self.scale=torch.nn.Parameter(torch.ones(3))
        def reset_classifier(self,n):pass
        def forward(self,x):return x.mean((2,3))*self.scale
    class Model(torch.nn.Module):
        def __init__(self,backbone=None):
            super().__init__();self.img_size=4;self.mean=(.5,)*3;self.std=(.5,)*3;self.backbone=Backbone();self.head=torch.nn.Linear(3,1)
        def forward(self,x):return self.head(self.backbone(x)).squeeze(-1)
    monkeypatch.setattr(timm,'create_model',lambda *a,**kw:Backbone())
    monkeypatch.setattr(cm,'ContentHeadModel',Model)
    monkeypatch.setattr(cm,'_SHARED',{})
    monkeypatch.setattr(arms,'INPUT_SIZE',4)
    monkeypatch.setattr(arms,'_device',lambda:'cpu')
    monkeypatch.setattr(arms,'connect_local',lambda:pytest.fail('offline trainer must not connect'))
    labels={i:int(i<15) for i in range(60)}
    media=[];root=tmp_path/'media';root.mkdir()
    for i,label in labels.items():
        f=io.BytesIO();Image.new('RGB',(7,5),'white' if label else 'black').save(f,format='PNG');body=f.getvalue()
        (root/f'{i}.bin').write_bytes(body)
        media.append({'id':i,'blob_sha256':hashlib.sha256(body).hexdigest(),'exclusion':None})
    doc={'schema':p.SCHEMA,'tag':'test','status':'prepared','graph_nodes':list(labels),'graph_edges':[],
      'gt':[{'id':i,'verdict':'positive' if v else 'negative','derived_from':'human_correction','media_exclusion':None} for i,v in labels.items()],
      'floor_snapshot':{'min_precision':.85,'min_recall':.7,'min_val_pos_support':3,'min_pred_pos_support':3},
      'seed':1,'parameters':{'positive':3,'negative':10,'tolerance':.1},'grouping_policy':p.POLICY,'media':media}
    doc['split']=p.fixed_split(labels,{i:i for i in labels},positive=3,negative=10,seed=1)
    doc=p.write_once(tmp_path/'snapshot.json',doc)
    p.write_once(root/'dataset.json',{'schema':'dedicated-dataset/1','snapshot_sha256':doc['sha256'],
      'images':{str(x['id']):x['blob_sha256'] for x in media}})
    def fit(getv,labels,groups,split,**kw):
        # The fitting implementation has its own leakage tests; here prove the
        # outer path passes the frozen split unchanged and bundles before scoring.
        assert split==doc['split']
        return {'w':torch.ones(3),'b':torch.tensor(0.)},.5,{}
    monkeypatch.setattr(p,'fit_candidate',fit)
    out=tmp_path/'trial'/'result.json'
    result=p.measure('test',tmp_path/'snapshot.json',out,media_dir=root)
    bundle=torch.load(str(out)+'.bundle.pt',weights_only=False)
    assert result['bundle_sha256']==hashlib.sha256(Path(str(out)+'.bundle.pt').read_bytes()).hexdigest()
    assert bundle['meta']['preprocessing'] == result['feature_contract']['preprocessing']
    assert result['heldout']['fp']==0 and result['heldout']['fn']==0
    assert result['registration_eligible'] is False
    assert set(result['heldout_scores'])=={str(i) for i in doc['split']['heldout']}

    from tagger.dedicated_qualification import qualify
    qualified=qualify(tmp_path/'snapshot.json',out,root,tmp_path/'qualified.json',
        baseline=str(out)+'.bundle.pt',baseline_sha256=result['bundle_sha256'],baseline_threshold=.5)
    assert qualified['parity']['passed']
    assert qualified['parity']['decision_differences']==0
    assert qualified['heldout']==qualified['baseline_diagnostic']
    assert qualified['state']=='awaiting_production_precision'
    assert qualified['promotion_eligible'] is False

    first=qualify(tmp_path/'snapshot.json',out,root,tmp_path/'first-head.json')
    assert first['parity']['passed'] and first['quality_floor']['passed']
    assert first['baseline_diagnostic'] is None
    assert first['baseline_training_overlap']=='unavailable'
    assert first['promotion_eligible'] is False


def test_export_resumes_partial_dataset_and_verifies_existing_bytes(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from tagger import display_bytes, vnext_arms

    doc, body = world(tmp_path, monkeypatch)
    root = tmp_path / 'dataset'
    root.mkdir()
    (root / '1.bin').write_bytes(body)
    doc['split']['heldout'] = [2]
    doc['media'].append({'id': 2, 'blob_sha256': hashlib.sha256(b'next').hexdigest()})
    snapshot = tmp_path / 'snapshot.json'
    del doc["sha256"]
    doc = p.write_once(snapshot, dict(doc, schema=p.SCHEMA))
    monkeypatch.setattr(vnext_arms, 'connect_primary', Mock())
    fetch = Mock(return_value=[(2, b'next')])
    monkeypatch.setattr(display_bytes, 'load', fetch)

    with pytest.raises(p.ProtocolError, match='byte budget exceeded'):
        ds.export(snapshot, root, resume=True, max_bytes=len(body))
    assert not (root / 'dataset.json').exists()
    assert not (root / '2.bin').exists()

    result = ds.export(snapshot, root, resume=True)
    assert result['bytes'] == len(body) + 4
    assert fetch.call_args.args[1] == [2]
    assert ds.FrozenImages(root, doc)([1, 2]) == [(1, body), (2, b'next')]
