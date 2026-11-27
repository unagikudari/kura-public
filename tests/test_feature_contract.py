import io
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
import torch
from PIL import Image
from tagger import feature_contract as fc, vnext_arms as arms
from tagger.content_preprocessing import transform, fingerprint


@pytest.fixture
def world(monkeypatch,tmp_path):
    calls=[];blobs={};weight=[1.0]
    class Backbone(torch.nn.Module):
        num_features=3
        def __init__(self):
            super().__init__();self.weight=torch.nn.Parameter(torch.full((3,),float(weight[0])))
        def reset_classifier(self,n):pass
        def forward(self,x):
            calls.append(len(x));return x.mean((2,3))*self.weight
    import timm
    monkeypatch.setattr(timm,'create_model',lambda *a,**kw:Backbone())
    monkeypatch.setattr(arms,'INPUT_SIZE',4)
    monkeypatch.setattr(arms,'_device',lambda:'cpu')
    monkeypatch.setenv('VNEXT_EMB_BATCH','2')
    monkeypatch.setattr(arms.display_bytes,'load',lambda query,ids:[(i,blobs[i]) for i in ids if i in blobs])
    def image(pid,color):
        f=io.BytesIO();Image.new('RGB',(7,5),color).save(f,format='PNG');blobs[pid]=f.getvalue()
    image(1,(10,20,30));image(2,(80,90,100))
    def extract():return fc.extract(None,[1,2],tmp_path/'legacy.pt',classifier_reset=True,label='test')
    return SimpleNamespace(calls=calls,blobs=blobs,weight=weight,image=image,extract=extract,
                           Backbone=Backbone,path=tmp_path/'legacy.pt')


def test_legacy_cache_ignored_and_input_changes_invalidate(world):
    world.path.write_bytes(b'legacy cache is deliberately not a torch file')
    a,e=world.extract();assert world.calls==[2]
    b,e2=world.extract();assert world.calls==[2]
    assert e==e2 and torch.equal(a[1],b[1])
    world.image(2,(200,210,220))
    c,e3=world.extract();assert world.calls==[2,1]
    assert torch.equal(a[1],c[1]) and not torch.equal(a[2],c[2])
    assert e3['input_sha256']['2']!=e['input_sha256']['2']
    assert world.path.read_bytes().startswith(b'legacy')


def test_backbone_change_uses_another_cache(world):
    _,a=world.extract();world.weight[0]=2
    _,b=world.extract()
    assert a['cache_key']!=b['cache_key'] and world.calls==[2,2]


def test_bad_cache_or_backbone_fails_closed(world):
    _,e=world.extract();p=Path(e['cache_path']);cache=torch.load(p,weights_only=True)
    cache['identity']['preprocessing_sha256']='bad';torch.save(cache,p)
    with pytest.raises(ValueError,match='identity mismatch'):world.extract()
    p.unlink();Path(e['backbone_path']).write_bytes(b'invalid')
    with pytest.raises(Exception):world.extract()


def test_interrupted_checkpoint_does_not_publish_partial_cache(world,monkeypatch):
    original=arms._save_feature_cache
    def fail(data,path):
        if 'features' in data:raise OSError('disk full')
        original(data,path)
    monkeypatch.setattr(arms,'_save_feature_cache',fail)
    with pytest.raises(OSError):world.extract()
    assert not list(world.path.parent.glob('*.contract-v1/*.pt')) or all(
        p.name.endswith('.backbone.pt') for p in world.path.parent.glob('*.contract-v1/*.pt'))


def test_actual_extraction_frozen_bundle_and_worker_parity(world,monkeypatch,tmp_path):
    from tagger import content_head_model as cm, nodec_autotag_worker as worker
    from tagger.vnext_arms import _image_transform
    class Head(torch.nn.Module):
        def __init__(self,backbone=None):
            super().__init__();self.backbone=world.Backbone();self.head=torch.nn.Linear(3,1)
            self.img_size=4;self.mean=(.5,)*3;self.std=(.5,)*3
        def forward(self,x):return self.head(self.backbone(x)).squeeze(-1)
    monkeypatch.setattr(cm,'ContentHeadModel',Head);monkeypatch.setattr(cm,'_SHARED',{})
    monkeypatch.setattr(worker,'DEVICE','cpu')
    features,e=world.extract()
    model,meta=fc.attach_head(torch.tensor([.2,.3,.4]),torch.tensor(.1),e)
    path=tmp_path/'bundle.pt';torch.save({'model':model.state_dict(),'meta':meta},path)
    loaded=cm.load(path)
    for pid,blob in world.blobs.items():
        expected=float(torch.sigmoid(model.head(features[pid]))[0].detach())
        assert worker._content_head_prob(Image.open(io.BytesIO(blob)),loaded)==pytest.approx(expected,abs=1e-7)
    assert meta['preprocessing']['normalization']=={'mean':[.5]*3,'std':[.5]*3}
    import copy
    bad=copy.deepcopy(meta);bad['feature_identity']['backbone_sha256']='bad'
    bad_path=tmp_path/'bad.pt';torch.save({'model':model.state_dict(),'meta':bad},bad_path)
    with pytest.raises(ValueError,match='feature identity'):cm.load(bad_path)
    # Bundling cannot fetch newer HF weights after extraction.
    world.weight[0]=9
    model2,_=fc.attach_head(torch.tensor([.2,.3,.4]),torch.tensor(.1),e)
    assert fc.state_hash(model2.backbone.state_dict())==e['identity']['backbone_sha256']



def test_measurement_bundle_emission_has_contract(world,monkeypatch,tmp_path):
    from tagger import content_head_model as cm
    class Head(torch.nn.Module):
        def __init__(self,backbone=None):
            super().__init__();self.backbone=world.Backbone();self.head=torch.nn.Linear(3,1)
            self.img_size=4;self.mean=(.5,)*3;self.std=(.5,)*3
        def forward(self,x):return self.head(self.backbone(x)).squeeze(-1)
    monkeypatch.setattr(cm,'ContentHeadModel',Head)
    monkeypatch.setenv('KURA_ARTIFACT_DIR',str(tmp_path/'artifacts'))
    monkeypatch.setattr(arms,'fit_deployable',lambda *a,**kw:{'w':torch.tensor([.2,.3,.4]),'b':torch.tensor(.1)})
    features,e=world.extract()
    out={'classification':{'verdict':'deployable','at':'feat1024@test'},'feature_contract':e}
    arms._save_deployable_head(out,'test',{'feat1024':lambda i:features[i]},[1],[2],42)
    bundle=torch.load(out['artifact_ref'],weights_only=True)
    assert bundle['meta']['feature_identity']==e['identity']
    assert bundle['meta']['requires_preprocessing_review']
