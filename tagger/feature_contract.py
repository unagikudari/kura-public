"""Content-addressed feature identity and explicit preprocessing for new trials.

Legacy post-id-only caches are never read. Every requested media blob is hashed
before reuse. The exact extraction backbone is frozen for subsequent bundling.
"""
from __future__ import annotations
import fcntl
import hashlib
import io
import json
import time
from pathlib import Path
from .content_preprocessing import spec, fingerprint, transform


def state_hash(state):
    h=hashlib.sha256()
    for name,tensor in sorted(state.items()):
        h.update(json.dumps([name,str(tensor.dtype),list(tensor.shape)],separators=(',',':')).encode())
        h.update(tensor.detach().cpu().contiguous().numpy().tobytes())
    return h.hexdigest()


def identity(state, contract, classifier_reset):
    from importlib.metadata import version
    return {'schema':'kura-feature-contract/1', 'backbone_sha256':state_hash(state),
            'preprocessing':contract, 'preprocessing_sha256':fingerprint(contract),
            'classifier_reset':classifier_reset,
            'libraries':{p:version(p) for p in ('torch','torchvision','timm','Pillow')},
            'implementation_sha256':hashlib.sha256(
                Path(__file__).read_bytes()+Path(__file__).with_name('content_preprocessing.py').read_bytes()).hexdigest()}


def extract(conn, ids, base_path, *, classifier_reset, label, image_loader=None):
    import torch,timm
    from PIL import Image
    from . import vnext_arms as arms
    contract=spec(arms.INPUT_SIZE,[.5]*3,[.5]*3) if classifier_reset else spec(arms.INPUT_SIZE)
    model=timm.create_model('hf-hub:SmilingWolf/wd-swinv2-tagger-v3',pretrained=True)
    if classifier_reset:model.reset_classifier(0)
    frozen={k:v.detach().cpu().clone() for k,v in model.state_dict().items()}
    ident=identity(frozen,contract,classifier_reset)
    key=hashlib.sha256(json.dumps(ident,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    root=Path(base_path).parent/(Path(base_path).stem+'.contract-v1')
    root.mkdir(parents=True,exist_ok=True)
    cache_path=root/(key+'.pt')
    backbone_path=root/(ident['backbone_sha256']+'.backbone.pt')
    requested=list(dict.fromkeys(ids));result={};input_hashes={}
    transform_image=transform(contract)
    device=arms._device();batch_size=int(arms.os.environ.get('VNEXT_EMB_BATCH','8'))
    if batch_size<1:raise ValueError('batch size must be positive')
    with (root/(key+'.lock')).open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        cache=torch.load(cache_path,weights_only=True) if cache_path.exists() else {
            'identity':ident,'inputs':{},'features':{}}
        if cache.get('identity')!=ident:raise ValueError('feature cache identity mismatch')
        if classifier_reset:
            if backbone_path.exists():
                if state_hash(torch.load(backbone_path,weights_only=True))!=ident['backbone_sha256']:
                    raise ValueError('frozen backbone hash mismatch')
            else:arms._save_feature_cache(frozen,backbone_path)
        del frozen
        model.eval().to(device)
        last_save=time.monotonic();dirty=False
        try:
            for start in range(0,len(requested),batch_size):
                chunk=requested[start:start+batch_size]
                rows=(image_loader(chunk) if image_loader is not None else
                      arms.display_bytes.load(lambda sql,a:arms.q(conn,sql,a),chunk))
                available={pid:bytes(data) for pid,data in rows}
                todo=[];images=[]
                for pid in chunk:
                    data=available.get(pid)
                    if data is None:continue
                    digest=hashlib.sha256(data).hexdigest()
                    if cache['inputs'].get(pid)==digest and pid in cache['features']:
                        result[pid]=cache['features'][pid];input_hashes[pid]=digest
                        continue
                    try:image=transform_image(Image.open(io.BytesIO(data)))
                    except (OSError,ValueError):continue
                    images.append(image);todo.append((pid,digest))
                if images:
                    with torch.inference_mode():values=model(torch.stack(images).to(device)).float().cpu()
                    values=values.clone()  # ordinary tensors for downstream head fitting
                    for (pid,digest),value in zip(todo,values):
                        cache['features'][pid]=value;cache['inputs'][pid]=digest
                        result[pid]=value;input_hashes[pid]=digest
                    dirty=True
                print(f'  {label} contract {min(start+batch_size,len(requested))}/{len(requested)}',flush=True)
                if dirty and time.monotonic()-last_save>=30:
                    arms._save_feature_cache(cache,cache_path);last_save=time.monotonic();dirty=False
            if dirty:arms._save_feature_cache(cache,cache_path)
        finally:
            del model
            if device=='cuda':torch.cuda.empty_cache()
    evidence={'identity':ident,'cache_key':key,'cache_path':str(cache_path),
              'backbone_path':str(backbone_path) if classifier_reset else None,
              'input_sha256':{str(k):v for k,v in input_hashes.items()}}
    return result,evidence


def attach_head(w,b,evidence):
    """No new HF lookup: bundle the very weights used for feature extraction."""
    import torch
    from .content_head_model import ContentHeadModel,WD_SWINV2
    ident=evidence['identity']
    if ident['schema']!='kura-feature-contract/1' or ident['classifier_reset'] is not True:
        raise ValueError('not an embedding extraction contract')
    if fingerprint(ident['preprocessing'])!=ident['preprocessing_sha256']:
        raise ValueError('preprocessing identity mismatch')
    state=torch.load(evidence['backbone_path'],map_location='cpu',weights_only=True)
    if state_hash(state)!=ident['backbone_sha256']:raise ValueError('frozen backbone changed')
    model=ContentHeadModel(backbone=WD_SWINV2)
    model.backbone.load_state_dict(state,strict=True)
    with torch.no_grad():
        model.head.weight.copy_(torch.as_tensor(w).reshape_as(model.head.weight))
        model.head.bias.copy_(torch.as_tensor(b).reshape_as(model.head.bias))
    model.eval()
    return model,{'backbone':WD_SWINV2,'preprocessing':ident['preprocessing'],
                  'feature_identity':ident, 'requires_preprocessing_review':True}
