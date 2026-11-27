import io

import pytest
import torch
from PIL import Image
from torchvision import transforms as T
from tagger.content_preprocessing import spec, validate, fingerprint, transform, for_model


def test_contract_is_copied_and_fingerprinted():
    a = spec(32, [.5]*3, [.5]*3)
    b = validate(a)
    assert fingerprint(a) == fingerprint(b)
    b['normalization']['std'][0] = .25
    assert a['normalization']['std'][0] == .5
    assert fingerprint(a) != fingerprint(b)
    assert fingerprint(a) != fingerprint(spec(32))


@pytest.mark.parametrize('change', [
    lambda x: x.update(schema='future/2'),
    lambda x: x.update(unknown=True),
    lambda x: x.update(size=[True, 32]),
    lambda x: x.update(exif='transpose'),
    lambda x: x.update(normalization={'mean':[0]*3,'std':[0]*3}),
    lambda x: x.update(normalization={'mean':[float('nan')]*3,'std':[1]*3}),
])
def test_invalid_contract_fails_closed(change):
    a=spec(32);change(a)
    with pytest.raises(ValueError):validate(a)


@pytest.mark.parametrize('mode', ['RGB','RGBA','L'])
def test_legacy_inference_and_extraction_preserved(mode):
    from tagger.vnext_arms import _image_transform, INPUT_SIZE
    image = Image.new(mode, (43, 21), 91)
    legacy = T.Compose([T.Resize((32,32)),T.ToTensor(),T.Normalize([.5]*3,[.5]*3)])
    assert torch.equal(legacy(image.convert('RGB')),transform(spec(32,[.5]*3,[.5]*3))(image))
    extraction = T.Compose([T.Resize((INPUT_SIZE,INPUT_SIZE)),T.ToTensor()])
    assert torch.equal(extraction(image.convert('RGB')), _image_transform()(image))


@pytest.mark.parametrize('legacy_first', [False, True])
@pytest.mark.parametrize('normalized', [False, True])
def test_extraction_bundle_and_both_serving_paths_match(monkeypatch, tmp_path, normalized, legacy_first):
    from tagger import content_head_model as loader, nodec_autotag_worker as worker, infer
    from tagger.vnext_arms import _image_transform
    # Tiny local backbone: no downloads, realistic state_dict/bundle/load path.
    class Tiny(torch.nn.Module):
        def __init__(self, backbone=None):
            super().__init__()
            self.backbone_name=backbone
            self.backbone=torch.nn.Sequential(torch.nn.AdaptiveAvgPool2d(1), torch.nn.Flatten())
            self.backbone.num_features=3
            self.head=torch.nn.Linear(3,1)
            self.img_size=32; self.mean=(.5,)*3; self.std=(.5,)*3
        def forward(self,x):return self.head(self.backbone(x)).squeeze(-1)
    monkeypatch.setattr(loader, 'ContentHeadModel', Tiny)
    monkeypatch.setattr(loader, '_SHARED', {})
    monkeypatch.setattr(worker,'DEVICE','cpu')
    # First model in shared cache has another contract. Second must not inherit it.
    model=Tiny(loader.WD_SWINV2)
    first=tmp_path/'first.pt'
    torch.save({'model':model.state_dict(),'meta':{'backbone':loader.WD_SWINV2,
        **({} if legacy_first else {'preprocessing':spec(32)})}},first)
    loaded_first=loader.load(first)
    assert loaded_first.preprocessing_legacy is legacy_first
    assert for_model(loaded_first)==(spec(32,[.5]*3,[.5]*3) if legacy_first else spec(32))
    contract=spec(32,[.5]*3,[.5]*3) if normalized else spec(32)
    path=tmp_path/'test.pt'
    torch.save({'model':model.state_dict(),'meta':{'backbone':loader.WD_SWINV2,
        'preprocessing':contract}},path)
    loaded=loader.load(path)
    assert loaded.backbone is loaded_first.backbone
    assert for_model(loaded)==contract
    image=Image.new('RGBA',(43,21),(15,89,201,40))
    image.getexif()[274]=6  # contract explicitly retains historical ignore behavior
    data=io.BytesIO();image.save(data,format='PNG',exif=image.getexif())
    x=_image_transform(contract)(image).unsqueeze(0)
    assert torch.equal(x,transform(for_model(loaded))(image).unsqueeze(0))
    expected=float(torch.sigmoid(model(x))[0].detach())
    assert worker._content_head_prob(image,loaded)==pytest.approx(expected,abs=1e-7)
    assert infer._run_content_head(data.getvalue(),loaded)==pytest.approx(expected,abs=1e-7)
    # Explicitly malformed metadata must not silently select legacy defaults.
    torch.save({'model':model.state_dict(),'meta':{'preprocessing':None}},path)
    with pytest.raises(ValueError):loader.load(path)

    torch.save({'model':model.state_dict(),'meta':{'backbone':loader.WD_SWINV2,
        'preprocessing':spec(33)}},path)
    with pytest.raises(ValueError, match='size'):loader.load(path)


@pytest.mark.parametrize('api', ['no_enum_no_antialias', 'enum_no_antialias', 'enum_and_antialias'])
@pytest.mark.parametrize('mode', ['RGB','RGBA','L'])
@pytest.mark.parametrize('size', [7, 47])
def test_historical_resize_api_matches_old_pil_compose(monkeypatch,api,mode,size):
    # Emulate historical constructor signatures, while exercising real PIL
    # bilinear resampling and real ToTensor/Normalize. No old torch install.
    calls=[]
    class OldResize:
        def __init__(self,size,interpolation=Image.Resampling.BILINEAR):
            self.size=size;self.interpolation=interpolation
            calls.append({'interpolation':interpolation})
        def __call__(self,image):
            assert isinstance(image,Image.Image)
            return image.resize((self.size[1],self.size[0]),Image.Resampling.BILINEAR)
    class NewResize(OldResize):
        def __init__(self,size,interpolation=Image.Resampling.BILINEAR,antialias=False):
            super().__init__(size,interpolation);calls[-1]['antialias']=antialias
    enum=getattr(T,'InterpolationMode')
    monkeypatch.setattr(T,'Resize',NewResize if api=='enum_and_antialias' else OldResize)
    if api=='no_enum_no_antialias':monkeypatch.delattr(T,'InterpolationMode')
    # Nonconstant, non-square data with edges/gradient; both up/down sampling.
    import numpy as np
    pixels=(np.arange(29*19*3,dtype=np.uint16).reshape(19,29,3)*17%256).astype('uint8')
    image=Image.fromarray(pixels).convert(mode)
    expected=T.Compose([T.Resize((size,size)),T.ToTensor(),T.Normalize([.5]*3,[.5]*3)])(image.convert('RGB'))
    actual=transform(spec(size,[.5]*3,[.5]*3))(image)
    assert torch.equal(actual,expected)
    if api!='no_enum_no_antialias':assert calls[-1]['interpolation']==enum.BILINEAR
    if api=='enum_and_antialias':assert calls[-1]['antialias'] is True


def test_non_pil_input_is_rejected_before_resize(monkeypatch):
    class Resize:
        def __init__(self,size):pass
        def __call__(self,image):raise AssertionError('must reject before resize')
    monkeypatch.setattr(T,'Resize',Resize)
    monkeypatch.delattr(T,'InterpolationMode')
    with pytest.raises(TypeError,match='PIL'):
        transform(spec(16))(torch.zeros(3,16,16))
