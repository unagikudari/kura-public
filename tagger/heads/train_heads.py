#!/usr/bin/env python3
"""wd14-swinv2 backbone (1024-dim, patch head と同形式) 上で新概念 head を学習。
production 適用は別途配線。ここは train+validate、成果物は patch 形式で保存。"""
import os,io,json,sys,random,hashlib
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF","expandable_segments:True")
import torch,numpy as np,timm
from PIL import Image
from safetensors.torch import load_file, save_file
from tempfile import NamedTemporaryFile
from pathlib import Path

# REPO ROOT ON sys.path. This file is always launched BY PATH — update_cycle.sh runs
# `$PY "$HERE/train_heads.py"` with WorkingDirectory=~/tagger-heads — so sys.path[0]
# is tagger/heads/ and the `tagger` package is invisible. Nothing supplies it from
# outside either: the unit sets no PYTHONPATH and kura is not pip-installed into the
# cycle's venv. The `from tagger.heads import ...` lines below therefore need this,
# exactly as the siblings that already import that way do (promote_verified.py,
# backfill_verified_provenance.py). Adding those imports WITHOUT this line is what
# killed the 6h cycle for ~24h on 2026-09-05 (signal tagger-head-update/trainer,
# count=125): the failure is deterministic at startup, not load-dependent.
# tests/test_heads_scripts_can_import_their_package.py holds this shape.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# ARTIFACT ROOT. Code is versioned (kura repo); artifacts are NOT — models, logs and
# ledgers stay outside the repo and their location is configuration, not a literal.
# The default is where they live today, so this change moves nothing.
DATA=os.environ.get("TAGGER_HEADS_DATA", os.path.expanduser("~/tagger-heads"))
from tagger.kura_db import connect_read  # noqa: E402  bulk reads (tagger/kura_db.py)
from tagger.heads.canonical_gt import coverage, load_inputs
from tagger.heads import planner as _pl
from tagger.heads.quality import (
    APPLY_THRESHOLDS, NO_UPDATE, independent_validation, measure, rejection, training_ids_for,
)

samples, floors = load_inputs()
counts = coverage(samples, floors)
print("HEADS_GT", json.dumps({"floors": floors, "tags": counts}, sort_keys=True), flush=True)
plan = {}
results = {}
existing_training = {}
heads_root = Path(DATA) / "heads"


def report():
    print("HEADS_RESULT", json.dumps(results, sort_keys=True), flush=True)


for tag, labels in samples.items():
    if counts[tag]["need_positive"] or counts[tag]["need_negative"]:
        results[tag] = {"state": "skipped", "reason": "below_gt_floor", **counts[tag]}
        continue
    if any(not _pl.split_by_post(list(ids))[0] for ids in labels.values()):
        results[tag] = {"state": "skipped", "reason": "insufficient_split"}
        continue
    _, proposed_val = _pl.split_by_post(list(labels["positive"]))
    weight_path = heads_root / f"{tag}.safetensors"
    if weight_path.exists():
        prior_training = training_ids_for(weight_path, heads_root / "manifests" / f"{tag}.manifest.json")
        if prior_training is None:
            results[tag] = {"state": "skipped", "reason": "existing_training_provenance_unavailable",
                            "positive_upper_bound": len(proposed_val),
                            "minimum_positive": floors["min_val_pos_support"]}
            continue
        existing_training[tag] = prior_training
    independent_pos = independent_validation(proposed_val, existing_training.get(tag, ()))
    if len(independent_pos) < floors["min_val_pos_support"]:
        results[tag] = {"state": "skipped", "reason": "insufficient_validation_positive",
                        "observed": len(independent_pos), "minimum": floors["min_val_pos_support"]}
        continue
    plan[tag] = (list(labels["positive"]), list(labels["negative"]))
if not plan:
    report()
    raise SystemExit(NO_UPDATE)

conn = connect_read()
conn.autocommit = True


def rows(sql, params):
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


dev="cuda"
print("loading wd14-swinv2 backbone...",flush=True)
bb=timm.create_model("hf-hub:SmilingWolf/wd-swinv2-tagger-v3",pretrained=True)
bb.reset_classifier(0); bb.eval().to(dev)
for p in bb.parameters(): p.requires_grad=False
cfg=timm.data.resolve_data_config({},model=bb)
tf=timm.data.create_transform(**cfg)
print("transform:",cfg,flush=True)

# GPU の同居相手は増える。2026-08-02 に vlm-serving (Qwen2.5-VL-7B、常駐 ~7GB) が
# 載り、bs=64 の埋め込みが CUDA OOM で落ちて **6 時間ごとのサイクルが 2 日間死んだ**
# (rc=1 だけが journal に出て、本文は cycle.log に埋もれていた)。
# 固定 batch は「今の同居状況」を暗黙の前提にしている — 前提が変わると落ちる。
# OOM は**遅くなる理由**であって止まる理由ではないので、batch を半分にして続ける。
EMBED_BS = int(os.environ.get("EMBED_BS", "64"))
EMBED_BS_FLOOR = int(os.environ.get("EMBED_BS_FLOOR", "1"))

def _embed_batch(chunk):
    """1 バッチを埋め込む。OOM したら呼び出し側が batch を割る。"""
    imgs=[];keep=[]
    from tagger import display_bytes
    for pid,data in display_bytes.load(rows,chunk):
        try: im=Image.open(io.BytesIO(data)).convert("RGB")
        except: continue
        imgs.append(tf(im));keep.append(pid)
    if not imgs: return {}
    x=torch.stack(imgs).to(dev)
    with torch.no_grad(): f=bb(x).float().cpu()
    return dict(zip(keep,f))

def embed(ids,bs=None):
    res={}
    bs = bs or EMBED_BS
    i = 0
    while i < len(ids):
        chunk = ids[i:i+bs]
        try:
            res.update(_embed_batch(chunk))
        except torch.OutOfMemoryError:
            torch.cuda.empty_cache()
            if bs <= EMBED_BS_FLOOR:
                raise   # ここまで割っても入らないなら本当に容量が無い — 黙って諦めない
            bs = max(EMBED_BS_FLOOR, bs // 2)
            print(f"  embed OOM -> batch を {bs} に落として続行 "
                  f"(同居プロセスが VRAM を握っている)", flush=True)
            continue    # 同じ i を新しい bs でやり直す
        i += bs
        print(f"  embed {min(i,len(ids))}/{len(ids)} (bs={bs})",flush=True)
    return res

allids = {pid for pos, neg in plan.values() for pid in pos + neg}

print("total unique imgs to embed:",len(allids),flush=True)
emb=embed(sorted(allids))

def atomic_write(dest, writer):
    with NamedTemporaryFile(dir=dest.parent, prefix=f".{dest.name}.", delete=False) as temp:
        staged = Path(temp.name)
    try:
        writer(staged)
        os.replace(staged, dest)
    finally:
        staged.unlink(missing_ok=True)


OUT={}
for name,(pos,neg) in plan.items():
    # 以降の索引は「emb にある id の並び」に一致させる。tensor の位置ではなく id で引く。
    # 80/20 split — **post_id 単位で、埋め込みより前に確定した順序**を使う (round 1 指摘)。
    # tensor の添字を shuffle すると post の同一性が消え、派生画像が train/val を跨いでも
    # 分からない。planner が決めた split を id で引き当てる。
    _pids=[i for i in pos if i in emb]; _nids=[i for i in neg if i in emb]
    ptr_ids,pva_ids=_pl.split_by_post(_pids); ntr_ids,nva_ids=_pl.split_by_post(_nids)
    if (len(_pids) < floors["min_gt_positive"] or len(_nids) < floors["min_gt_negative"]
            or not all((ptr_ids, pva_ids, ntr_ids, nva_ids))):
        results[name] = {"state": "skipped", "reason": "insufficient_loaded_samples",
                         "positive": len(_pids), "negative": len(_nids)}
        continue
    prior_training = existing_training.get(name, ())
    pva_ids = independent_validation(pva_ids, prior_training)
    nva_ids = independent_validation(nva_ids, prior_training)
    if len(pva_ids) < floors["min_val_pos_support"] or not nva_ids:
        results[name] = {"state": "skipped", "reason": "insufficient_independent_validation",
                         "positive": len(pva_ids), "negative": len(nva_ids),
                         "minimum_positive": floors["min_val_pos_support"]}
        continue
    ntr_ids = ntr_ids[:2 * len(ptr_ids)]
    Xp=torch.stack([emb[i] for i in _pids])
    Xn=torch.stack([emb[i] for i in _nids])
    _pl.assert_no_split_leak(name, ptr_ids+ntr_ids, pva_ids+nva_ids)
    _pi={i:j for j,i in enumerate(_pids)}; _ni={i:j for j,i in enumerate(_nids)}
    Xtp=Xp[[_pi[i] for i in ptr_ids]]; Xvp=Xp[[_pi[i] for i in pva_ids]]
    Xtn=Xn[[_ni[i] for i in ntr_ids]]; Xvn=Xn[[_ni[i] for i in nva_ids]]
    Xtr=torch.cat([Xtp,Xtn]);ytr=torch.cat([torch.ones(len(Xtp)),torch.zeros(len(Xtn))])
    Xv=torch.cat([Xvp,Xvn]);yv=torch.cat([torch.ones(len(Xvp)),torch.zeros(len(Xvn))])
    # normalize features (like patch space)
    mu=Xtr.mean(0);sd=Xtr.std(0)+1e-6
    Xtr=(Xtr-mu)/sd
    w=torch.zeros(1024,requires_grad=True);b=torch.zeros(1,requires_grad=True)
    opt=torch.optim.Adam([w,b],lr=0.05);lf=torch.nn.BCEWithLogitsLoss(
        pos_weight=torch.tensor(float(len(Xtn))/max(1,len(Xtp))), reduction="none")
    for _ in range(500):
        opt.zero_grad()
        per=lf(Xtr@w+b,ytr)                       # reduction="none" -> 標本ごと
        loss=per.mean()+1e-3*w.norm()**2
        loss.backward();opt.step()
    threshold = APPLY_THRESHOLDS.get(name, 0.5)
    candidate = {"w": w.detach(), "b": b.detach(), "mu": mu, "sd": sd}
    dest = Path(DATA) / "heads" / f"{name}.safetensors"
    existing = name in existing_training
    baseline = None
    with torch.no_grad():
        try:
            measured = measure(candidate, Xv, yv, threshold)
        except ValueError:
            results[name] = {"state": "rejected", "reason": "invalid_candidate"}
            continue
        if existing:
            try:
                if training_ids_for(dest, heads_root / "manifests" / f"{name}.manifest.json") == prior_training:
                    baseline = measure(load_file(dest), Xv, yv, threshold)
            except Exception as exc:
                print(f"[{name}] existing head unreadable ({type(exc).__name__})", flush=True)
    why = rejection(measured, floors, existing=existing, baseline=baseline)
    results[name] = {"state": "rejected" if why else "accepted", "reason": why,
                     "candidate": measured, "baseline": baseline, "threshold": threshold}
    if why:
        continue
    OUT[name] = dict(training_ids=ptr_ids+ntr_ids, used_ids={"positive": ptr_ids+pva_ids, "negative": ntr_ids+nva_ids},
                     P=measured["precision"], R=measured["recall"], acc=measured["accuracy"],
                     pos=len(_pids), neg=len(ntr_ids)+len(nva_ids), **candidate)

# 保存 (patch 形式 + メタ) — validate 用の scratch dir
if not OUT:
    report()
    raise SystemExit(NO_UPDATE)
os.makedirs(f"{DATA}/heads",exist_ok=True)
summary={}
for name,d in OUT.items():
    weight_path = heads_root / f"{name}.safetensors"
    atomic_write(weight_path,
                 lambda path: save_file({key: d[key] for key in ("w", "b", "mu", "sd")}, path))
    summary[name]={k:d[k] for k in ("P","R","acc","pos","neg")}
    manifest = {
        "schema": "kura-head-training/1",
        "source": "tagger_gt_label",
        "head_sha256": hashlib.sha256(weight_path.read_bytes()).hexdigest(),
        "training_post_ids": sorted(d["training_ids"]),
        "chosen": [
            {"post_id": pid, "verdict": verdict, "audit_id": samples[name][verdict][pid]}
            for verdict, ids in d["used_ids"].items() for pid in ids
        ],
    }
    manifest_dir = Path(DATA) / "heads" / "manifests"
    manifest_dir.mkdir(exist_ok=True)
    manifest_path = manifest_dir / f"{name}.manifest.json"
    manifest_text = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=1)
    atomic_write(manifest_path, lambda path: path.write_text(manifest_text))
    summary[name]["manifest"] = {"source": manifest["source"]}
    summary[name]["manifest_ref"] = {
        "path": str(manifest_path),
        "sha256": hashlib.sha256(manifest_text.encode()).hexdigest(),
        "chosen_count": len(manifest["chosen"]),
    }
atomic_write(Path(DATA) / "heads" / "summary.json",
             lambda path: path.write_text(json.dumps(summary, indent=1)))
report()
print("HEADS_DONE",json.dumps(summary),flush=True)
