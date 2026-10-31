#!/usr/bin/env python3
"""3 head を universe にスコアし ≥TH を機械適用(complete 経路=provenance/INV-M/audit)。
検証 pool は作らない(人間ノルマゼロ)。適用は applied_log.jsonl に記録(GT 純度維持用)。"""
import os,io,json,urllib.request,ssl,base64,datetime,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tagger import display_bytes
from tagger.api_target import api_base, api_origin
from tagger.kura_db import connect_read
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF","expandable_segments:True")
import torch,psycopg2,timm
from PIL import Image
from safetensors.torch import load_file

# per-concept 閾値: 7341 検証の実測精度に基づく (majora 97.1% / minora 79.6% / wrinkled 82.0%)
# 弱い head は高閾値で適用し、hard-neg 還流で head が育ったら下げる
TH_BY={"dark_labia_majora":0.95,"dark_labia_minora":0.995,"wrinkled_labia":0.98}
TH=0.95  # 後方互換 (pool description 用)
SRC=os.environ.get("APPLY_SRC","trainer-head-r3")
HOME=os.path.expanduser("~/tagger-heads")
CONCEPTS=["dark_labia_majora","dark_labia_minora","wrinkled_labia"]
HEADS={c:load_file(f"{HOME}/heads/{c}.safetensors") for c in CONCEPTS}
ctx=ssl.create_default_context();ctx.check_hostname=False;ctx.verify_mode=ssl.CERT_NONE
AUTH={"Authorization":"Token "+base64.b64encode(f"owner:{os.environ['KTOK']}".encode()).decode(),
 "Content-Type":"application/json","Accept":"application/json"}
BASE=api_base()
def api(m,p,b=None):
    d=json.dumps(b).encode() if b is not None else None
    try:return json.load(urllib.request.urlopen(urllib.request.Request(BASE+p,data=d,headers=AUTH,method=m),timeout=30,context=ctx))
    except Exception as e:return {"_err":repr(e)[:80]}
conn=connect_read();conn.autocommit=True   # bulk reads (tagger/kura_db.py); writes go through the API
def rows(sql,a=None):
    c=conn.cursor();c.execute(sql,a);r=c.fetchall();c.close();return r

print("load wd14...",flush=True)
bb=timm.create_model("hf-hub:SmilingWolf/wd-swinv2-tagger-v3",pretrained=True)
bb.reset_classifier(0);bb.eval().to("cuda")
cfg=timm.data.resolve_data_config({},model=bb);tf=timm.data.create_transform(**cfg)

# 過去に機械適用済み(再適用しない)
applied_before=set()
logf=f"{HOME}/applied_log.jsonl"
if os.path.exists(logf):
    for line in open(logf):
        try:
            d=json.loads(line); applied_before.add((d["c"],d["pid"]))
        except: pass

uni=rows("""select p.id, array(select (select name from tag_name where tag_id=pt.tag_id order by ord limit 1) from post_tag pt where pt.post_id=p.id) from post p
 where exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=p.id and tn.name='dark_labia') and exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=p.id and tn.name='uncensored')
 and p.type='image' and p.media_location = 'pg' and not exists(select 1 from tagger_gt_exclude e where e.post_id=p.id)""")
print(f"universe={len(uni)}",flush=True)
tagmap={pid:set(t) for pid,t in uni}
allids=[pid for pid,_ in uni]

todo={c:[] for c in CONCEPTS}
# GPU の同居相手は増える (2026-08-02 に vlm-serving Qwen2.5-VL-7B が常駐 ~7GB)。
# 固定 batch は「今の同居状況」を暗黙の前提にしており、前提が変われば落ちる。
# OOM は**遅くなる理由**であって止まる理由ではないので batch を割って続ける。
# train_heads.py の embed() と同じ扱い (片方だけ直すと、もう片方で同じ日に落ちる)。
BS=int(os.environ.get("APPLY_BS","64"))
BS_FLOOR=int(os.environ.get("APPLY_BS_FLOOR","1"))

def _score_chunk(chunk):
    imgs=[];keep=[]
    for pid,data in display_bytes.load(rows,chunk):
        try: im=Image.open(io.BytesIO(data)).convert("RGB")
        except: continue
        imgs.append(tf(im));keep.append(pid)
    if not imgs: return
    x=torch.stack(imgs).to("cuda")
    with torch.no_grad(): f=bb(x).float().cpu()
    for c in CONCEPTS:
        H=HEADS[c];fn=(f-H["mu"])/H["sd"]
        p=torch.sigmoid(fn@H["w"]+H["b"])
        for pid,s in zip(keep,p.tolist()):
            if s>=TH_BY.get(c,TH) and c not in tagmap[pid] and (c,pid) not in applied_before:
                todo[c].append(pid)

i=0;_n=0
while i<len(allids):
    try:
        _score_chunk(allids[i:i+BS])
    except torch.OutOfMemoryError:
        torch.cuda.empty_cache()
        if BS<=BS_FLOOR: raise   # ここまで割って入らないなら本当に容量が無い
        BS=max(BS_FLOOR,BS//2)
        print(f"  score OOM -> batch を {BS} に落として続行 (同居プロセスが VRAM を握っている)",flush=True)
        continue
    i+=BS;_n+=1
    if _n%40==1: print(f"  scored {min(i,len(allids))}/{len(allids)} (bs={BS})",flush=True)

from collections import defaultdict
per_post=defaultdict(list)
for c,ids in todo.items():
    for pid in ids: per_post[pid].append(c)
print(f"apply対象: {len(per_post)} posts / "+", ".join(f"{c}:{len(todo[c])}" for c in CONCEPTS),flush=True)
ok=fail=0
now=datetime.datetime.now().isoformat()
with open(logf,"a") as lf:
    for n,(pid,cs) in enumerate(sorted(per_post.items())):
        # host: 構造化来歴 (migration 0072)。release_id は省略=NULL:
        # trainer head は linear-probe safetensors で tagger_release レジストリに行を持たない
        # (存在しない id を送ると FK 違反で INSERT 500)。
        r=api("POST","/internal/tagging/complete",
          {"id":pid,"kind":"auto_tag","status":"ok","tags":[{"name":c} for c in cs],"source":SRC,"host":"trainer"})
        if "_err" in r: fail+=1
        else:
            ok+=1
            for c in cs: lf.write(json.dumps({"c":c,"pid":pid,"src":SRC,"t":now})+"\n")
        if (n+1)%500==0: print(f"  applied {n+1}/{len(per_post)}",flush=True)
print(f"APPLIED posts ok={ok} fail={fail}",flush=True)
# 検証 pool 自動生成 — サンプル元は「未検証 backlog 全体」(applied − verified)。
# 新規適用が 0 でも未検証が残っていれば pool が出る(検証完了→新pool のフロー保証)
verified=set()
_vf=f"{HOME}/verified_log.jsonl"
if os.path.exists(_vf):
    for line in open(_vf):
        try:
            d=json.loads(line); verified.add(d["pid"])
        except: pass
backlog=set()
for line in open(logf):
    try:
        d=json.loads(line)
        if d["pid"] not in verified: backlog.add(d["pid"])
    except: pass
# rate-limit: 未検証(pending, 未promote)の labia HEADVERIFY pool が既にあれば新規生成しない
_unverified=0
if os.path.exists(f"{HOME}/pending_pools.jsonl"):
    for _l in open(f"{HOME}/pending_pools.jsonl"):
        try:
            _e=json.loads(_l)
            if _e.get("src","").startswith("trainer-head") and not _e.get("promoted") and not _e.get("retired"): _unverified+=1
        except: pass
if _unverified>=1:
    print(f"rate-limit: 未検証 labia pool {_unverified}件 → 新規検証pool 生成をスキップ(機械適用は実施済)",flush=True)
    backlog=set()
if backlog:
    import random as _r
    # 新規適用を優先し、残りを backlog で埋める
    new_ids=[p for p in per_post.keys() if p in backlog]
    rest=[p for p in backlog if p not in per_post]
    POOL_N=20  # 検証プールサイズ(user: 集中力が持つ 1/4 サイズ、2026-07-18)
    pool_src=(new_ids+_r.Random(11).sample(rest,len(rest)))[:POOL_N] if new_ids else _r.Random(11).sample(sorted(backlog),min(POOL_N,len(backlog)))
    sample=pool_src
    stamp=datetime.datetime.now().strftime("%m%d-%H%M")
    body={"names":[f"HEADVERIFY-{stamp}: 機械適用の検証"],"category":"default",
     "verify_status":"pending","tagger_source":SRC,
     "description":f"head 機械適用(source={SRC}, ≥{TH})の検証 pool。誤りを剥がすだけでOK。検証完了後 promote_verified.py が『残した=正例/剥がした=hard negative』を学習GTへ還流。","posts":sample}
    r=api("POST","/pools",body)
    if r.get("id"):
        with open(f"{HOME}/pending_pools.jsonl","a") as pf:
            pf.write(json.dumps({"pool":r["id"],"src":SRC,"t":now,"n":len(sample)})+"\n")
        print(f"VERIFY POOL {r.get("id")} URL={api_origin()}/?q=pool:{r.get("id")}",flush=True)
print("AUTOAPPLY_DONE",flush=True)
