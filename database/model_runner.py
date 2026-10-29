#!/usr/bin/env python3
"""model runner — 画像を VLM に見せ、構造化観測を model 名義で kura1 に commit する。

plan kura1-model-observation P1。**推論は trainer の endpoint、編成と DB 書き込みは
primary (ここ)** — cross-host DB 書き込みを構造的に消す (review 96bb34b2)。

流れ (1 scene):
  1. kura1 から画像を取り、data-URI にする (primary にある。trainer へ複製しない)
  2. 語彙を**領域ごとに分けて**複数パスで問う (species/presentation → 部位 → …)。
     559 語を平文で渡すと逸脱・切り詰めが起きる (review 96bb34b2 #1)。
  3. 各パスは JSON Schema を prompt に添え、返った JSON を検証
  4. 全パスを 1 つの ann にまとめ、annotate_scene.validate → write(producer='model')

**素案であってタグではない**: 非捏造をモデルにも課す (判らない軸は空)。
confidence は DB に入れず、生の応答は runner ログに残す (no-JSON + P0 分析用)。

使い方:
  python3 database/model_runner.py --scenes 648990,206141 --producer qwen2.5-vl-7b@base
  python3 database/model_runner.py --pool 7990 --mock   # endpoint 無しで経路検証
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import pathlib
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import annotate_scene as A  # noqa: E402
import psycopg  # noqa: E402

DSN = os.environ.get("KURA1_DSN", "postgresql://desk@127.0.0.1:5541/kura1")
ENDPOINT = os.environ.get("VLM_ENDPOINT", "http://127.0.0.1:8020/v1/chat/completions")
DATA_IMAGES = pathlib.Path(os.environ.get(
    "KURA_DATA_IMAGES",
    pathlib.Path(__file__).resolve().parent.parent / "data-images" / "posts"))

# 語彙を問う領域 (パス) の順。各パスは JSON の一部だけを埋めさせる。
# 判定基準そのものである各語の説明は prompt に添える (migration が書いた線が採点基準)。
PASSES = [
    ("identity", "各人物の species と presentation (性器が見えないカットでは "
                 "presentation は androgynous のまま断定しない)"),
    ("regions",  "各人物について画像で見えている body_region"),
    ("poses",    "各人物の pose"),
    ("relations","人物・部位どうしの relation (誰の何が誰の何に)"),
]


def _config_fingerprint() -> tuple[str, str]:
    """runner の構成 = repo HEAD (dirty 印つき) と語彙 migration 先頭。"""
    import subprocess
    repo = pathlib.Path(__file__).resolve().parent.parent
    run = lambda *args: subprocess.run(["git", "-C", str(repo), *args],  # noqa: E731
                                       capture_output=True, text=True).stdout.strip()
    # dirty 判定は database/ に限定 — repo 内の無関係な変更 (tagger 等、別作業の
    # 所有物) で版 fingerprint が汚れると、無用な版名バーンが起きる
    commit = (run("rev-parse", "--short", "HEAD") or "unknown") + \
        ("-dirty" if run("status", "--porcelain", "--untracked-files=no",
                         "--", "database/") else "")
    mig = sorted((repo / "database" / "migrations").glob("0*.sql"))
    return commit, (mig[-1].name[:4] if mig else "0000")


def register_or_verify_version(cur, producer: str) -> None:
    """producer_ref が構成 (コード × 語彙) を一意に決める invariant (0086)。

    未登録なら現在の構成で登録。登録済みで構成が違えば**実行を拒否** —
    同じ版名の中身が変わることを機構で封じる (版名 bump を忘れられない)。
    """
    commit, head = _config_fingerprint()
    cur.execute("SELECT runner_commit, ontology_head FROM observer_version"
                " WHERE producer_ref = %s", (producer,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO observer_version(producer_ref, model_ref,"
                    " runner_commit, ontology_head, note) VALUES (%s,%s,%s,%s,%s)",
                    (producer, producer.split("@")[0], commit, head,
                     f"auto-register (commit {commit} / 語彙 {head})"))
        print(f"observer_version 登録: {producer} = commit {commit} / 語彙 {head}")
    elif tuple(row) != (commit, head):
        raise SystemExit(
            f"拒否: 版 {producer} は commit {row[0]} / 語彙 {row[1]} で登録済みだが"
            f"現在は {commit} / {head} — 構成が変わっている。新しい版名を切ること (0086)")


def image_data_uri(post_id: int) -> str | None:
    """画像を data-URI に。**既存の image_path ヘルパに寄せる** (blob の実パスを引く)。"""
    from annotate_server import image_path
    hits = image_path(post_id)
    if not hits:
        return None
    b = hits[0].read_bytes()
    mime = "image/webp" if hits[0].suffix == ".webp" else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(b).decode()


def ask(data_uri: str, prompt: str, schema: dict, mock: bool = False) -> dict:
    if mock:
        return {}   # mock は「モデルは何も書かなかった」= 非捏造の既定
    body = {
        "model": "qwen2.5-vl-7b",
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": data_uri}},
            {"type": "text", "text": prompt + "\n\nJSON schema:\n"
             + json.dumps(schema, ensure_ascii=False)
             + "\n\n判らない軸は空にする (推測しない)。JSON だけ返す。"},
        ]}],
        "temperature": 0, "max_tokens": 1500,
        "response_format": {"type": "json_object"},  # Qwen 公式推奨 (無視されても害なし)
    }
    req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    txt = d["choices"][0]["message"]["content"]
    # ```json フェンスを剥がす
    txt = txt.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(txt)
    except Exception:
        return {"_raw": txt, "_parse_error": True}


_VOCAB = None


def vocab() -> dict:
    global _VOCAB
    if _VOCAB is None:
        import urllib.request
        u = os.environ.get("KURA1_VOCAB") or f"http://[{os.environ.get('KURA1_ANNOTATE_HOST', '::1')}]:8099/api/vocab"
        with urllib.request.urlopen(u, timeout=20) as r:
            _VOCAB = json.load(r)
    return _VOCAB


def _match(val, allowed: set):
    """モデルの語を語彙に**厳密一致**で写す。合わなければ None (=書かない、非捏造)。"""
    if not val:
        return None
    v = str(val).strip().lower().replace(" ", "_")
    return v if v in allowed else None


def observe(cur, post_id: int, producer: str, mock: bool, log) -> dict:
    """1 scene を観測して ann を組む。**まだ commit しない** (呼び手が検証してから)。"""
    uri = image_data_uri(post_id)
    if not uri:
        return {"error": f"画像が無い ({post_id})"}
    ann = {"kura_post_id": post_id, "annotator": "model-runner",
           "producer_kind": "model", "producer_ref": producer,
           "minutes_unmeasured": "model",
           "subjects": [], "relations": [], "scene_descriptors": [],
           "entity_counts": [], "unresolved": []}
    if mock:
        return ann
    V = vocab()
    sp_ok = {s["slug"] for s in V["species"]}
    pres_ok = set(V["presentation"])
    reg_ok = {r["slug"] for r in V["region"]}

    # ── identity パス: 人物ごとの species / presentation ──
    sp_list = " ".join(sorted(sp_ok))
    q = ("画像の**人物 (person / 獣人 等) を左から順に** person_1, person_2, … と数え、"
         "各人物の species と presentation を返す。"
         f"\nspecies は次の語からのみ選ぶ (無ければ human): {sp_list}。"
         "\npresentation は female/male/androgynous/ambiguous/other から。"
         "**性器が見えないカットでは male/female を断定せず androgynous**。"
         '\n形式: {"people":[{"ref":"person_1","species":"...","presentation":"..."}]}')
    idout = ask(uri, q, {"people": [{"ref": "person_1", "species": "", "presentation": ""}]}, mock)
    log.write(json.dumps({"post": post_id, "pass": "identity", "out": idout},
                         ensure_ascii=False) + "\n")
    for i, per in enumerate((idout.get("people") or [])[:8], 1):
        ref = f"person_{i}"
        sp = _match(per.get("species"), sp_ok) or "human"
        pres = _match(per.get("presentation"), pres_ok)
        s = {"ref": ref, "kind": "person",
             "identity": {"entity_kind": "person", "species": sp},
             "regions": [], "attributes": [], "poses": [], "objects": []}
        if pres:
            s["presentation"] = pres
        ann["subjects"].append(s)
    if not ann["subjects"]:
        return ann

    # ── entity_count パス: 人数を presentation × 参加/傍観で数える ──
    # (S2 で「runner にパスが無く構造ゼロ」だった実装ギャップの解消、user 指示 2026-08-01)
    pres_list = "/".join(sorted(pres_ok))
    qc = ("画像に写る人物を presentation ごとに数える。"
          f"\npresentation は {pres_list} から。"
          "\n**行為に加わっている人 (participant) と、見ているだけの背景の観衆"
          " (bystander) は分けて**数える。"
          "\n正確に数えられるなら exact=true、「少なくとも N 人」しか言えないなら false。"
          '\n形式: {"counts":[{"presentation":"female","count":1,"exact":true,'
          '"involvement":"participant"}]}')
    cout = ask(uri, qc, {"counts": []}, mock)
    log.write(json.dumps({"post": post_id, "pass": "entity_count", "out": cout},
                         ensure_ascii=False) + "\n")
    for cnt in (cout.get("counts") or [])[:8]:
        pres = _match(cnt.get("presentation"), pres_ok)
        try:
            n_ = int(cnt.get("count"))
        except (TypeError, ValueError):
            continue
        if not pres or n_ < 0:
            continue
        e = {"presentation": pres, "count_min": n_,
             "count_exact": bool(cnt.get("exact", True))}
        inv = _match(cnt.get("involvement"), {"participant", "bystander"})
        if inv:
            e["involvement"] = inv
        ann["entity_counts"].append(e)

    # ── regions パス: 人物ごとに見えている body_region ──
    refs = " ".join(s["ref"] for s in ann["subjects"])
    reg_list = " ".join(sorted(reg_ok))
    q2 = (f"画像の人物 ({refs}) それぞれについて、**その場面で意味のある主要な身体部位**"
          "だけを返す (見えている全部ではなく、行為・接触・露出に関わる部位)。"
          f"\n部位は次の語からのみ (無ければ空): {reg_list}。"
          '\n形式: {"regions":{"person_1":["face","breast",...]}}')
    rout = ask(uri, q2, {"regions": {"person_1": []}}, mock)
    log.write(json.dumps({"post": post_id, "pass": "regions", "out": rout},
                         ensure_ascii=False) + "\n")
    rmap = rout.get("regions") or {}
    for s in ann["subjects"]:
        got = rmap.get(s["ref"]) or []
        s["regions"] = [m for m in (_match(x, reg_ok) for x in got) if m]

    # ── anchor パス: 頭の中心点。**複数人のときだけ** (1 人なら不要、UI と同じ規則)。
    # 位置が入ると採点器の anchor 最近傍対応付け (fact 30fab4d1) が実装可能になる。
    # 座標は 0-1000 整数で聞いて 0..1 に正規化 (Qwen 系の grounding 慣例)。
    if len(ann["subjects"]) >= 2:
        qa = (f"各人物 ({refs}) の**頭の中心**の位置を返す。"
              "\n座標は画像の左上を (0,0)、右下を (1000,1000) とする整数。"
              '\n形式: {"anchors":{"person_1":{"x":420,"y":180}}}')
        aout = ask(uri, qa, {"anchors": {"person_1": {"x": 0, "y": 0}}}, mock)
        log.write(json.dumps({"post": post_id, "pass": "anchor", "out": aout},
                             ensure_ascii=False) + "\n")
        amap = aout.get("anchors") or {}
        for s in ann["subjects"]:
            p = amap.get(s["ref"]) or {}
            try:
                x, y = float(p.get("x")), float(p.get("y"))
            except (TypeError, ValueError):
                continue
            if 0 <= x <= 1000 and 0 <= y <= 1000:
                s["anchor"] = {"x": round(x / 1000, 4), "y": round(y / 1000, 4),
                               "region": "head"}

    # ── pose パス: 人物ごとの pose ──
    pose_ok = {x["slug"] for x in V["pose"]}
    pose_list = " ".join(sorted(pose_ok))
    q3 = (f"人物 ({refs}) それぞれの**体の構え (pose)** を返す。"
          f"\n次の語からのみ (無ければ空): {pose_list}。"
          '\n形式: {"poses":{"person_1":["sitting",...]}}')
    pout = ask(uri, q3, {"poses": {"person_1": []}}, mock)
    log.write(json.dumps({"post": post_id, "pass": "poses", "out": pout},
                         ensure_ascii=False) + "\n")
    pmap = pout.get("poses") or {}
    for s in ann["subjects"]:
        for pz in (pmap.get(s["ref"]) or []):
            m = _match(pz, pose_ok)
            if m:
                s["poses"].append({"pose": m})

    # ── relation パス: 誰の何が誰の何に ──
    rel_ok = {x["slug"] for x in V["relation"]}
    rel_list = " ".join(sorted(rel_ok))
    q4 = ("人物・部位どうしの**関係**を返す (誰の何が / 何を / 誰の何に)。"
          f"\n関係は次の語からのみ: {rel_list}。ref は "
          + ",".join(s["ref"] for s in ann["subjects"])
          + "。部位は先の語彙から。無ければ空。"
          '\n形式: {"relations":[{"relation":"touching","subject":{"ref":"person_1","region":"hand"},'
          '"object":{"ref":"person_2","region":"face"}}]}')
    relout = ask(uri, q4, {"relations": []}, mock)
    log.write(json.dumps({"post": post_id, "pass": "relations", "out": relout},
                         ensure_ascii=False) + "\n")
    valid_refs = {s["ref"] for s in ann["subjects"]}
    for r in (relout.get("relations") or [])[:20]:
        rname = _match(r.get("relation"), rel_ok)
        if not rname:
            continue
        sub = r.get("subject") or {}
        obj = r.get("object") or {}
        if sub.get("ref") not in valid_refs:
            continue
        oref = obj.get("ref") if obj.get("ref") in valid_refs else ""
        oreg = _match(obj.get("region"), reg_ok) or ""
        osurf = _match(obj.get("surface"), {x["slug"] for x in (V.get("surface") or [])})
        # **ref が無ければ region も落とす** (region は「誰の region」なので ref 必須)。
        # 環境 (surface) が対象なら ref/region は空。どちらも無ければ関係を捨てる
        if not oref:
            oreg = ""
        if not (oref or osurf):
            continue
        subreg = _match(sub.get("region"), reg_ok) or ""
        rr = {"relation": rname,
              "subject": {"ref": sub["ref"], "region": subreg},
              "object": {"ref": oref, "region": oreg}}
        if osurf and not oref:
            rr["object"]["surface"] = osurf
        ann["relations"].append(rr)

    # ── attribute パス: 人物の**見えている部位に付く属性**だけを問う ──
    # 属性は部位スコープなので、その人物が挙げた region に該当する属性だけを候補にする。
    # applies_kind='region' はその部位、'any_region' はどの部位でも、'entity' は人物全体。
    attr_meta = {a["slug"]: a for a in V["attribute"]}
    for s in ann["subjects"]:
        regs = set(s["regions"])
        # この人物で問える属性は **region_attrs (server 側の解決) を単一ソース**に読む
        # (0085)。region/any_region/class の解決を runner が再実装していた版は
        # class 属性 (flush_intensity, hair_density) を一度も問えていなかった。
        askable = []
        for rg in s["regions"]:
            for slug in (V.get("region_attrs") or {}).get(rg, []):
                a = attr_meta.get(slug)
                if a:
                    askable.append((a, rg))
        for a in V["attribute"]:
            if a["applies_kind"] == "entity" and a["applies_key"] == "person":
                askable.append((a, None))
        if not askable:
            continue
        # **重基準の属性 (値に判定基準を持つ) は 1 属性ずつ孤立プロンプトで問う**
        # (S0、arXiv 2503.16868 + 実測: 一括だと基準が希薄化し breast_size が large に落ちる。
        # 孤立なら huge=正)。部位ゲートで数は絞られる。軽い軸 (基準無し) は一括。
        heavy, light = [], []
        seen_a = set()
        for a, rg in askable:
            # region/entity 属性は 1 回だけ。any_region と class は部位ごとに問う
            if a["slug"] in seen_a and a["applies_kind"] not in ("any_region", "class"):
                continue
            seen_a.add(a["slug"])
            has_crit = a["values"] and sum(1 for v in a["values"] if v.get("d")) >= 2
            (heavy if has_crit else light).append((a, rg))

        def commit_attr(aout, only=None):
            for a in (aout.get("attributes") or [])[:20]:
                aslug = a.get("attribute")
                if only and aslug != only:
                    continue
                meta = attr_meta.get(aslug)
                if not meta:
                    continue
                # 連続値属性 (0..1) はモデルの数値を numeric へ。素通しすると
                # value も numeric も無い entry になり validate が scene ごと落とす
                # (@s3 で wetness が 2 scene 落とした実害。flush_intensity も同根)
                num = None
                if meta.get("value_type") == "continuous":
                    try:
                        num = float(a.get("numeric") if a.get("numeric") is not None
                                    else a.get("value"))
                    except (TypeError, ValueError):
                        continue
                    if not (0.0 <= num <= 1.0):
                        continue
                val_ok = {v["slug"] for v in meta["values"]}
                av = _match(a.get("value"), val_ok)
                if meta["values"] and not av:
                    continue
                areg = _match(a.get("region"), regs)
                if meta["applies_kind"] in ("region", "any_region", "class") and not areg:
                    areg = meta["applies_key"] if meta["applies_key"] in regs else None
                    if not areg:
                        continue
                entry = {"attribute": aslug}
                if av:
                    entry["value"] = av
                if num is not None:
                    entry["numeric"] = round(num, 3)
                if areg:
                    entry["region"] = areg
                s["attributes"].append(entry)

        # 重基準: 1 属性 1 プロンプト (基準を集中して届ける)
        for a, rg in heavy:
            vlines = "; ".join(
                v["slug"] + (f" = {v['d'].replace(chr(10),' ')}" if v.get("d") else "")
                for v in a["values"])
            head = a["slug"] + (f" @{rg}" if rg else "")
            adesc = (a.get("d") or "").split(chr(10))[0][:90]
            q = (f'{s["ref"]} の {head} を、次の判定基準で選ぶ (無ければ空)。'
                 + (f"\n{head} とは: {adesc}" if adesc else "")
                 + f"\n値と基準: {vlines}"
                 + '\n形式 (JSON): {"attributes":[{"attribute":"' + a["slug"]
                 + '","value":"...","region":"' + (rg or "") + '"}]}')
            aout = ask(uri, q, {"attributes": []}, mock)
            log.write(json.dumps({"post": post_id, "pass": "attr1", "ref": s["ref"],
                                  "attr": a["slug"], "region": rg, "out": aout},
                                 ensure_ascii=False) + "\n")
            commit_attr(aout, only=a["slug"])

        # 軽い軸 (基準無し) は一括のまま
        if light:
            opts = []
            for a, rg in light:
                vals = "/".join(v["slug"] for v in a["values"]) or "(数値0..1)"
                opts.append(f'{a["slug"]}={vals}' + (f" @{rg}" if rg else ""))
            q5 = (f'{s["ref"]} の属性で確実に見て取れるものだけ返す (無ければ空)。'
                  "\n候補: " + " | ".join(opts[:40])
                  + '\n形式 (JSON): {"attributes":[{"attribute":"...","value":"...","region":"..."}]}')
            aout = ask(uri, q5, {"attributes": []}, mock)
            log.write(json.dumps({"post": post_id, "pass": "attr_light", "ref": s["ref"],
                                  "out": aout}, ensure_ascii=False) + "\n")
            commit_attr(aout)
    return ann


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenes", help="post_id をカンマ区切り")
    ap.add_argument("--pool", type=int)
    ap.add_argument("--producer", default="qwen2.5-vl-7b@base")
    ap.add_argument("--mock", action="store_true", help="endpoint を叩かず経路だけ検証")
    ap.add_argument("--allow-s11-pool", action="store_true",
                    help="round 4 (S-11 母集団) への書き込みを許可 (通常は禁止)")
    ap.add_argument("--dsn", default=DSN)
    a = ap.parse_args()

    logdir = pathlib.Path(os.environ.get("VLM_RUNNER_LOG",
                                           str(pathlib.Path.home() / "kura1" / "model-runner-logs")))
    logdir.mkdir(parents=True, exist_ok=True)
    log = (logdir / "raw.jsonl").open("a", encoding="utf-8")

    with psycopg.connect(a.dsn, autocommit=False) as conn, conn.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        if not a.mock:   # mock は経路検証なので版を登録しない
            register_or_verify_version(cur, a.producer)
            conn.commit()
        if a.scenes:
            posts = [int(x) for x in a.scenes.split(",")]
        elif a.pool:
            from annotate_server import pool_posts  # 既存のヘルパ
            posts = pool_posts(a.pool)
        else:
            print("--scenes か --pool が要る"); return 1

        # **round 4 (S-11 母集団) は注記のみ** (fact 56f856b5、user 判断 2026-07-31)。
        # DB 同居は汚染ではない — S-11 の採点器は producer='human' しか読まず、
        # モデル観測を人間が**採用しない限り** S-11 に入らない (準用の原則)。
        # アンカリングも無視できる (WD14 ループと同じ、user 判断)。以前は除外していた。
        if not a.allow_s11_pool:
            try:
                from annotate_server import pool_posts as _pp
                inpool = [x for x in posts if x in set(_pp(7989))]
                if inpool:
                    print(f"注記: round4 (S-11) の scene が {len(inpool)} 件含まれる。"
                          f"同居は無害 (採用しない限り S-11 に入らない): {inpool[:5]}")
            except Exception:
                pass

        done = 0
        ng = 0
        for pid in posts:
            try:
                ann = observe(cur, pid, a.producer, a.mock, log)
                if ann.get("error"):
                    print(f"  {pid}: {ann['error']}", flush=True); ng += 1; continue
                errs = A.validate(ann, A.Ontology(cur))
                if errs:
                    conn.rollback()
                    print(f"  {pid}: validate NG {errs[0][:70]}", flush=True); ng += 1; continue
                got = A.write(cur, ann)
                conn.commit()   # scene ごとに commit — 途中で落ちても残る
                done += 1
                print(f"  [{done}] {pid}: model 観測 {got['observation']} 件 "
                      f"(skip {got['skipped']})", flush=True)
            except Exception as e:
                conn.rollback()
                print(f"  {pid}: 例外 {type(e).__name__}: {str(e)[:80]}", flush=True); ng += 1
        print(f"NG/skip: {ng} 件", flush=True)
    log.close()
    print(f"完了: {done} scene を model 名義で commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
