#!/usr/bin/env python3
"""サイクル完了後に世代台帳へ1行追記(val 指標・applied 数・pool)。"""
import json,os,datetime
HOME=os.path.expanduser("~/tagger-heads")
gen=int(open(f"{HOME}/generation").read().strip())
src=f"trainer-head-g{gen}"
rec={"gen":gen,"src":src,"t":datetime.datetime.now().isoformat(timespec="minutes")}
try:
    summ=json.load(open(f"{HOME}/heads/summary.json"))
    rec["val"]={c:{"P":round(m["P"],3),"R":round(m["R"],3)} for c,m in summ.items()}
    rec["gt"]={c:m.get("pos") for c,m in summ.items()}
except Exception: pass
applied=0
try:
    for l in open(f"{HOME}/applied_log.jsonl"):
        if json.loads(l).get("src")==src: applied+=1
except Exception: pass
rec["applied"]=applied
try:
    pools=[json.loads(l) for l in open(f"{HOME}/pending_pools.jsonl") if l.strip()]
    if pools and pools[-1].get("src")==src: rec["pool"]=pools[-1]["pool"]
except Exception: pass
rec["measured_precision"]=None
with open(f"{HOME}/generations.jsonl","a") as f: f.write(json.dumps(rec)+"\n")
print(f"generation g{gen} recorded (applied={applied})")
