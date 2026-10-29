#!/usr/bin/env python3
"""promote 済みの検証 pool を世代経過後に削除(解放)。
GT は verified_log、保護は pin にあるので pool 自体は消してよい。
usage: retire_pools.py [--generations N] (default 3)"""
import os,sys,json,urllib.request,ssl,base64,datetime
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tagger.api_target import api_base  # noqa: E402
HOME=os.environ.get("TAGGER_HEADS_DATA", os.path.expanduser("~/tagger-heads"))


def _no_token():
    raise SystemExit(
        "KTOK is not set. The token is read from the environment only -- a secret\n"
        "reachable by file path is a secret that can be committed. Source it from\n"
        "your credential store before running (see tagger/heads/README.md).")

GEN=int(sys.argv[sys.argv.index("--generations")+1]) if "--generations" in sys.argv else 3
ctx=ssl.create_default_context();ctx.check_hostname=False;ctx.verify_mode=ssl.CERT_NONE
KTOK=os.environ.get("KTOK") or _no_token()
AUTH={"Authorization":"Token "+base64.b64encode(f"owner:{KTOK}".encode()).decode(),
 "Content-Type":"application/json","Accept":"application/json"}
pf=f"{HOME}/pending_pools.jsonl"
if not os.path.exists(pf): sys.exit(0)
entries=[json.loads(l) for l in open(pf) if l.strip()]
# 世代 = pending_pools 内の自分より後に作られた pool 数
kept=[];retired=0
for i,e in enumerate(entries):
    newer=len(entries)-1-i
    if e.get("promoted") and newer>=GEN and not e.get("retired"):
        pid=e["pool"]
        try:
            req=urllib.request.Request(f"{api_base()}/pools/{pid}",
                data=b"{}",headers=AUTH,method="DELETE")
            urllib.request.urlopen(req,timeout=30,context=ctx)
            e["retired"]=datetime.datetime.now().isoformat();retired+=1
            print(f"retired pool {pid} (promoted {e['promoted'][:10]}, {newer} generations old)")
        except Exception as ex:
            print(f"pool {pid} delete failed: {ex}")
    kept.append(e)
open(pf,"w").write("".join(json.dumps(e)+"\n" for e in kept))
print(f"RETIRED {retired} pools (>= {GEN} generations, promoted)")
