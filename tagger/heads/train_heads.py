#!/usr/bin/env python3
"""wd14-swinv2 backbone (1024-dim, patch head と同形式) 上で新概念 head を学習。
production 適用は別途配線。ここは train+validate、成果物は patch 形式で保存。"""
import os,io,json,sys,random
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF","expandable_segments:True")
import torch,numpy as np,psycopg2,timm
from PIL import Image
from safetensors.torch import save_file
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
conn=connect_read();conn.autocommit=True
def rows(sql,a=None):
    c=conn.cursor();c.execute(sql,a);r=c.fetchall();c.close();return r

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

CONCEPTS={
 "wrinkled_labia": dict(
    pos="select id from post where exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='wrinkled_labia') and not exists(select 1 from tagger_gt_exclude e where e.post_id=post.id) and type='image' and media_location = 'pg'",
    neg="select id from post where exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='uncensored') and exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='pussy') and not(exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='wrinkled_labia')) and not exists(select 1 from tagger_gt_exclude e where e.post_id=post.id) and type='image' and media_location = 'pg' order by md5(id::text) limit 500"),
 "dark_labia_majora": dict(
    pos="select id from post where exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='dark_labia_majora') and not exists(select 1 from tagger_gt_exclude e where e.post_id=post.id) and type='image' and media_location = 'pg'",
    neg="select id from post where exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='uncensored') and exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='pussy') and not(exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='dark_labia_majora')) and not(exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='dark_labia')) and not exists(select 1 from tagger_gt_exclude e where e.post_id=post.id) and type='image' and media_location = 'pg' order by md5(id::text) limit 500"),
 "dark_labia_minora": dict(
    pos="select id from post where exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='dark_labia_minora') and not exists(select 1 from tagger_gt_exclude e where e.post_id=post.id) and type='image' and media_location = 'pg'",
    neg="select id from post where exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='uncensored') and exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='pussy') and not(exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='dark_labia_minora')) and not(exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='dark_labia')) and not exists(select 1 from tagger_gt_exclude e where e.post_id=post.id) and type='image' and media_location = 'pg' order by md5(id::text) limit 500"),
 # phimosis は base WD14 タグが P≈0 のノイズ(mine_phimosis.py 参照)なので raw タグ query を pos に使わない。
 # GT は curated verified_log(mine_phimosis 手書き import + grey_owl seed pool の人手判定)由来 = gt:"verified"。
 # neg_fill は uncensored∩penis∩¬phimosis を hard-neg 補充に(2x まで)。
 "phimosis": dict(
    gt="verified",
    neg_fill="select id from post where exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='uncensored') and exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='penis') and not(exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='phimosis')) and not exists(select 1 from tagger_gt_exclude e where e.post_id=post.id) and type='image' and media_location = 'pg' order by md5(id::text) limit 500"),
 # small_penis / dark_penis = grey_owl tag-delta 逆算で判明した WD14 二大盲点。base タグはノイズ(small_penisは danbooru語彙, dark_penisは23297機械大量)なので curated verified_log GT で。
 "small_penis": dict(
    gt="verified",
    neg_fill="select id from post where exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='uncensored') and exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='penis') and not(exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='small_penis')) and not exists(select 1 from tagger_gt_exclude e where e.post_id=post.id) and type='image' and media_location = 'pg' order by md5(id::text) limit 500"),
 "dark_penis": dict(
    gt="verified",
    neg_fill="select id from post where exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='uncensored') and exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='penis') and not(exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='dark_penis')) and not exists(select 1 from tagger_gt_exclude e where e.post_id=post.id) and type='image' and media_location = 'pg' order by md5(id::text) limit 500"),
 # dark_perineum / half-erect = grey_owl tag-delta 逆算で浮上した追加盲点。curated verified_log GT。
 "dark_perineum": dict(
    gt="verified",
    neg_fill="select id from post where exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='uncensored') and exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='penis') and not(exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='dark_perineum')) and not exists(select 1 from tagger_gt_exclude e where e.post_id=post.id) and type='image' and media_location = 'pg' order by md5(id::text) limit 500"),
 "half-erect": dict(
    gt="verified",
    neg_fill="select id from post where exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='uncensored') and exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='penis') and not(exists(select 1 from post_tag pt join tag_name tn on tn.tag_id=pt.tag_id where pt.post_id=post.id and tn.name='half-erect')) and not exists(select 1 from tagger_gt_exclude e where e.post_id=post.id) and type='image' and media_location = 'pg' order by md5(id::text) limit 500"),
}

# 全 pos/neg id を集めて一括 embed (重複 embed 回避)
allids=set()
plan={}
import os as _os
_applied={}; _verpos={}; _verneg={}
_lf=f"{DATA}/applied_log.jsonl"
if _os.path.exists(_lf):
    for _line in open(_lf):
        try:
            _d=json.loads(_line);_applied.setdefault(_d["c"],set()).add(_d["pid"])
        except: pass
# kura#64: a verified_log row is curated ground truth only if it says so in the
# row — carries human provenance under the schema-2 ledger. A machine-applied row,
# a provenance-less legacy row, or a malformed one is a candidate, never a teacher,
# so it does NOT enter verified_pos/neg. Refusing them can only lose curated GT
# (training falls back to the SQL-derived set); it can never let a machine guess in
# as truth, which is the failure this issue was about. The count of refusals is
# logged once so "curated GT went empty" is visible, not silent — a legacy file
# that predates the backfill shows up here as a loud refusal, not as green.
from tagger.heads import verified_ledger as _vl
_vf=f"{DATA}/verified_log.jsonl"
# Four outcomes, counted apart on purpose (trainer's post-backfill observation):
#   curated  — human provenance, used as verified GT
#   machine  — schema-2 but source=machine_applied: correctly demoted, EXPECTED,
#              not a problem — a backfilled file is mostly these
#   legacy   — no `schema` field: a pre-#64 row the backfill has not converted yet
#   malformed— schema present but invalid (unknown source, bad shape)
# Only `legacy` means "run the backfill"; folding machine into it made the hint
# fire forever after a clean backfill.
_ver_curated=_ver_machine=_ver_legacy=_ver_malformed=0
if _os.path.exists(_vf):
    for _line in open(_vf):
        _line=_line.strip()
        if not _line:
            continue
        try:
            _d=json.loads(_line)
        except Exception:
            _ver_malformed+=1; continue
        if _vl.is_curated(_d):
            (_verpos if _d["label"]=="positive" else _verneg).setdefault(
                _d["concept"], set()).add(_d["post_id"])
            _ver_curated+=1
        elif not isinstance(_d, dict) or "schema" not in _d:
            _ver_legacy+=1                    # pre-#64 row, backfill pending
        elif _d.get("source") in _vl.MACHINE_SOURCES:
            _ver_machine+=1                   # demoted on purpose — expected, not an error
        else:
            _ver_malformed+=1                 # schema-2 but does not validate
print(f"  [verified_log] curated={_ver_curated} used; machine(excluded)={_ver_machine}"
      f"{f', legacy(RUN BACKFILL)={_ver_legacy}' if _ver_legacy else ''}"
      f"{f', malformed={_ver_malformed}' if _ver_malformed else ''}", flush=True)
if _ver_legacy:
    print(f"  [verified_log] {_ver_legacy} legacy rows lack provenance — run "
          f"tagger/heads/backfill_verified_provenance.py on the heads node (kura#64).",
          flush=True)
from tagger.heads import planner as _pl
import datetime as _dt

# 既存 8 タグの neg SQL は `limit 500` で、正例の多いタグでは予算 2*pos が 500 を超える
# (dark_labia_minora: 予算 451,252)。旧経路も同じ不足を抱えており、planner の既定 (止める)
# にすると本番 8 タグが全滅する — なので **期限付きで** 通す。期限が切れれば学習が止まり、
# LIMIT を上げる (Phase 2) か根拠を書いて更新するしかない。記録を読む人を待たない。
_LEGACY_SHORTFALL_WAIVER = _pl.ShortfallWaiver(
    reason="既存 8 タグの neg SQL は LIMIT 500。旧経路と同じ不足で、Phase 1 は挙動を変えない。"
           " LIMIT の引き上げ (完全候補集合) は Phase 2 の SQL 作業で行う",
    expires=_dt.date(2026, 10, 4),
    granted_by="plan kura-generic-head-trainer rev3 / 実装レビュー round 3 (両席 REVISE の fold)")

# 旧ループ (二分岐 + `neg[:len(pos)*2]`) は planner の dispatch 表と層別クォータへ移した。
# ここは SQL を撃って id 列を渡すだけ — 判断は planner にあり、そちらは DB 無しで
# テストできる。
_PLANS = {}
for name,q in CONCEPTS.items():
    _kind = _pl.kind_of(q)
    if _kind == "verified_log":
        _plan = _pl.build(name, q,
            verified_pos=list(_verpos.get(name,set())),
            verified_neg=list(_verneg.get(name,set())),
            neg_fill=[r[0] for r in rows(q["neg_fill"])] if q.get("neg_fill") else [],
            shortfall_waiver=_LEGACY_SHORTFALL_WAIVER)
    else:
        _plan = _pl.build(name, q,
            sql_pos=[r[0] for r in rows(q["pos"])],
            sql_neg=[r[0] for r in rows(q["neg"])],
            applied=list(_applied.get(name,set())),
            verified_pos=list(_verpos.get(name,set())),
            verified_neg=list(_verneg.get(name,set())),
            shortfall_waiver=_LEGACY_SHORTFALL_WAIVER)
    _PLANS[name] = _plan
    if _verpos.get(name) or _verneg.get(name):
        print(f"  [{name}] verified: +{len(_verpos.get(name,()))} pos,"
              f" +{len(_verneg.get(name,()))} hard-neg")
    plan[name]=(_plan.pos,_plan.neg); allids|=set(_plan.pos)|set(_plan.neg)

print("total unique imgs to embed:",len(allids),flush=True)
emb=embed(list(allids))

OUT={}
for name,(pos,neg) in plan.items():
    Xp=torch.stack([emb[i] for i in pos if i in emb])
    Xn=torch.stack([emb[i] for i in neg if i in emb])
    # 以降の索引は「emb にある id の並び」に一致させる。tensor の位置ではなく id で引く。
    # 80/20 split — **post_id 単位で、埋め込みより前に確定した順序**を使う (round 1 指摘)。
    # tensor の添字を shuffle すると post の同一性が消え、派生画像が train/val を跨いでも
    # 分からない。planner が決めた split を id で引き当てる。
    _pids=[i for i in pos if i in emb]; _nids=[i for i in neg if i in emb]
    ptr_ids,pva_ids=_pl.split_by_post(_pids); ntr_ids,nva_ids=_pl.split_by_post(_nids)
    _pl.assert_no_split_leak(name, ptr_ids+ntr_ids, pva_ids+nva_ids)
    _pi={i:j for j,i in enumerate(_pids)}; _ni={i:j for j,i in enumerate(_nids)}
    Xtp=Xp[[_pi[i] for i in ptr_ids]]; Xvp=Xp[[_pi[i] for i in pva_ids]]
    Xtn=Xn[[_ni[i] for i in ntr_ids]]; Xvn=Xn[[_ni[i] for i in nva_ids]]
    Xtr=torch.cat([Xtp,Xtn]);ytr=torch.cat([torch.ones(len(Xtp)),torch.zeros(len(Xtn))])
    # PER-SAMPLE WEIGHT。S2 (PU の弱ラベル) を確定負例と同じ重さで学習しないための器。
    # `reduction="none"` の損失に掛けてから平均する — `BCEWithLogitsLoss(weight=...)` に
    # 渡す形でも同じだが、正規化の仕方を明示したいので手で書く (round 1 指摘)。
    # 既存 8 タグは全負例が確定 (weight 1.0) なので、この経路を通しても**学習は変わらない**。
    _wneg = _PLANS[name].weights if name in _PLANS else [1.0]*len(neg)
    _wmap = dict(zip(neg, _wneg))
    wtr=torch.cat([torch.ones(len(Xtp)),
                   torch.tensor([_wmap.get(i,1.0) for i in ntr_ids],dtype=torch.float)])
    Xv=torch.cat([Xvp,Xvn]);yv=torch.cat([torch.ones(len(Xvp)),torch.zeros(len(Xvn))])
    # normalize features (like patch space)
    mu=Xtr.mean(0);sd=Xtr.std(0)+1e-6
    Xtr=(Xtr-mu)/sd; Xv=(Xv-mu)/sd
    w=torch.zeros(1024,requires_grad=True);b=torch.zeros(1,requires_grad=True)
    opt=torch.optim.Adam([w,b],lr=0.05);lf=torch.nn.BCEWithLogitsLoss(
        pos_weight=torch.tensor(float(len(Xtn))/max(1,len(Xtp))), reduction="none")
    for _ in range(500):
        opt.zero_grad()
        per=lf(Xtr@w+b,ytr)                       # reduction="none" -> 標本ごと
        loss=(per*wtr).sum()/wtr.sum()+1e-3*w.norm()**2   # 重み付き平均。正規化は sum(w)
        loss.backward();opt.step()
    with torch.no_grad():
        # val metrics @ th=0.5
        pv=torch.sigmoid(Xv@w+b)
        pred=(pv>0.5).float()
        tp=((pred==1)&(yv==1)).sum().item();fp=((pred==1)&(yv==0)).sum().item()
        fn=((pred==0)&(yv==1)).sum().item();tn=((pred==0)&(yv==0)).sum().item()
        P=tp/max(1,tp+fp);R=tp/max(1,tp+fn);acc=(tp+tn)/max(1,len(yv))
    print(f"[{name}] pos={len(Xp)} neg={len(Xn)} | val P={P:.2f} R={R:.2f} acc={acc:.2f} (tp{tp}/fp{fp}/fn{fn}/tn{tn})",flush=True)
    OUT[name]=dict(P=P,R=R,acc=acc,pos=len(Xp),neg=len(Xn),
                   w=w.detach(),b=b.detach(),mu=mu,sd=sd)

# 保存 (patch 形式 + メタ) — validate 用の scratch dir
os.makedirs(f"{DATA}/heads",exist_ok=True)
summary={}
for name,d in OUT.items():
    save_file({"w":d["w"],"b":d["b"],"mu":d["mu"],"sd":d["sd"]},f"{DATA}/heads/{name}.safetensors")
    summary[name]={k:d[k] for k in ("P","R","acc","pos","neg")}
    # 負例 provenance。round 2 で指摘されたとおり、集計だけ summary に載せて `chosen`
    # (post ごとの層・重み) を**どこにも保存していなかった** — 「manifest で再構成できる」
    # という主張が嘘になっていた。完全 manifest は別 artifact に落とし、summary には
    # 参照先と digest だけ置く (summary を全 ID で膨らませない、も round 1 の指摘)。
    if name in _PLANS:
        _man=_PLANS[name].manifest
        _mdir=f"{DATA}/heads/manifests"; os.makedirs(_mdir,exist_ok=True)
        _mpath=f"{_mdir}/{name}.manifest.json"
        _blob=json.dumps(_man,ensure_ascii=False,sort_keys=True,indent=1)
        open(_mpath,"w").write(_blob)
        summary[name]["manifest"]={k:v for k,v in _man.items() if k != "chosen"}
        summary[name]["manifest_ref"]={
            "path": _mpath,
            "sha256": __import__("hashlib").sha256(_blob.encode()).hexdigest(),
            "chosen_count": len(_man.get("chosen",[]))}
json.dump(summary,open(f"{DATA}/heads/summary.json","w"),indent=1)
print("HEADS_DONE",json.dumps(summary),flush=True)
