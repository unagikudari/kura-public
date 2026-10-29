#!/usr/bin/env python3
"""kura1 注釈 UI — plan kura-1.0-implementation S-8 の専用ページ。

なぜ独立サービスか:
  * production kura の `api/` `web/` に注釈用の経路を足したくない (無停止 invariant)。
  * kura1 は 127.0.0.1:5541 bind なので、書き込みは primary 上のプロセスから行う必要がある。
  * 画像は data-images をこのプロセスが直接読んで返す → kura の session cookie が不要。

検証と書き込みは `annotate_scene.py` の validate() / write() を **import して再利用**する
(語彙検証の実装を二重に持たない)。

起動: systemd user unit `kura1-annotate.service`
      http://<primary の tailscale アドレス>:8098/
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import sys
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 同じディレクトリの相棒を読む (repo の中に置いてあるので絶対パスに縛らない —
# kura 統合時にこのまま持っていけるようにするため)。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import annotate_scene as A  # noqa: E402  validate() / write() / Ontology を再利用
import derive_image_facts as _derive  # noqa: E402  画素から決まる事実 (向き / 色モード)

import psycopg  # noqa: E402

DSN = "host=127.0.0.1 port=5541 dbname=kura1 user=desk"

# 選択 UI のグループ名。117 の部位と 49 の pose を平坦に並べると探せないため、
# 部位は最上位の祖先で、pose は scope でまとめる (性交時の構えだけ category で別出し)。
REGION_GROUP = {
    # head 配下で REGION_OVERRIDE に無いものはここへ落ちる (取りこぼしの受け皿)
    "head": "顔のパーツ", "neck": "首", "torso": "胴体", "pelvis": "骨盤・股",
    "arm": "腕・手", "leg": "脚・足", "body": "全体",
}
# 見出しの並び順 (上から順に body を辿る解剖学的な順序。アルファベット順にしない)
REGION_ORDER = ["頭部", "顔", "顔のパーツ", "目", "口", "首", "胸", "胴体", "骨盤・股",
                "男性器", "女性器", "排泄器", "腕・手", "脚・足", "非人間の部位",
                "全体", "その他"]

# 祖先だけでは「外性器」が 1 つの塊 (26 語) になり探しにくい (2026-07-28 指摘)。
# 男性器 / 女性器 / 排泄器 は臨床的にも別系統なので、slug を明示して独立させる。
# ここに無い pelvis 配下は「骨盤・股」に残る (buttock / groin / mons / perineum 等)。
REGION_OVERRIDE = {
    **{r: "男性器" for r in (
        "penis", "penile_shaft", "glans", "foreskin", "frenulum", "scrotum", "testis")},
    **{r: "女性器" for r in (
        "vulva", "labia_majora", "labia_minora", "clitoris", "clitoral_hood",
        "vaginal_opening")},
    # 排泄器 = 排泄に関わる開口部とその周囲。urethral_opening は男女共通なのでここへ。
    **{r: "排泄器" for r in ("anus", "perianal_region", "urethral_opening")},
    # 胸は注釈頻度が飛び抜けて高いので独立させる (2026-07-28 指摘)。
    # 骨の目印 (sternum / rib) は胸郭であって「胸」ではないので 胴体 に残す。
    **{r: "胸" for r in ("chest", "breast", "areola", "nipple")},
    # 頭部・顔 が 34 語で最大だったので 4 つに割る (2026-07-29 指摘「face を独立
    # カテゴリにして」)。face だけ外しても「顔」が 29 語のままなので、外性器を
    # 3 分割したときと同じ形で **目と口も独立**させる — どちらも eye_state /
    # gaze_direction / mouth_state の置き場で、注釈頻度が高い。
    # 頭部 = 顔以外の頭 (5)、顔 = 顔面の造作 (15)、目 (7)、口 (7)。
    **{r: "頭部" for r in ("head", "scalp", "skull", "ear", "earlobe")},
    # **face だけを独立したリストにする** (2026-07-29 指摘)。顔面の造作 14 語と同じ
    # リストに埋もれていると、表情を書くための「顔がある」が探しにくい。
    # face の行は選択肢が 1 つなので、選べば即 facial_expression が下に出る。
    "face": "顔",
    **{r: "顔のパーツ" for r in (
        "forehead", "temple", "cheek", "cheekbone", "chin", "jaw", "jawline",
        "nose", "nose_bridge", "nostril", "eyebrow", "beard", "mustache", "sideburns")},
    **{r: "目" for r in ("eye", "iris", "pupil", "sclera",
                         "upper_eyelid", "lower_eyelid", "eyelash")},
    **{r: "口" for r in ("mouth", "upper_lip", "lower_lip", "inner_lip",
                         "teeth", "gums", "tongue")},
    # 0030: 非人間の付属肢。人体の部位と混ぜると探しにくいので独立させる。
    **{r: "非人間の部位" for r in (
        "tentacle", "tail", "wing", "horn", "claw", "animal_ear", "fin")},
}
# 部位に紐づかない属性 (applies_kind='entity') の **表示先** カテゴリ。
# pregnancy_state は人物全体の状態なので観測側では region を持たないが、腹の形から
# 判断するものなので「胴体」の箱に出す方が探しやすい (2026-07-28 指摘)。
# データモデルは変えない — obs_attribute.region_slug は NULL のまま。
ENTITY_ATTR_GROUP = {"pregnancy_state": "胴体",
                     # 震え・痙攣は体全体の状態 (0023)、体格も体全体 (0026)
                     "involuntary_motion": "全体",
                     "muscularity": "全体", "adiposity": "全体",
                     # 体の向きは人物ごとの事実 (0032)
                     "body_orientation": "全体",
                     # 0062: **排出は左パネルの「噴出物」で扱う。** 0053 で人物全体の
                     # 属性にしたが、「全体の属性」の箱に 4 つ並ぶと**何の話か判らない**
                     # (user 指摘 2026-07-31)。噴出物を主体に、行き先を言う関係の
                     # すぐ隣で扱う。`__` 始まりのグループはどの部位カテゴリとも
                     # 一致しないので、ここに入れると**どちらの箱からも消える**。
                     "urination_state": "__emission",
                     "defecation_state": "__emission",
                     "ejaculation_state": "__emission",
                     "squirting_state": "__emission"}
# 噴出物: 属性 (出ているか) と関係 (どこから どこへ) を **1 つの箱**で扱う (0062)
EMISSION_ATTRS = ["urination_state", "ejaculation_state",
                  "defecation_state", "squirting_state"]
EMISSION_RELS = ["urinating_onto", "ejaculating_onto",
                 "defecating_onto", "squirting_onto"]
POSE_ORDER = ["全体", "顔・頭の動き", "腕の動き", "手の動き", "脚の動き",
              "性交時の構え", "上半身", "下半身"]
POSE_GROUP = {
    "whole_body": "全体", "head": "顔・頭の動き", "arms": "腕の動き",
    "hands": "手の動き", "legs": "脚の動き",
    "upper_body": "上半身", "lower_body": "下半身",
}
# pose も部位に従属させる (2026-07-28 指摘)。pose は特定の region ではなく `scope`
# (動く体の部分) に付くので、**region の category に従属させる**のが唯一の正直な対応。
# `arm_up` を `hand` チップの下にぶら下げるのは DB に無い対応付けを捏造することになる。
POSE_REGION_GROUP = {
    # head scope の pose (head_tilt / looking_back 等) は頭の動きなので「頭部」へ
    "head": "頭部", "upper_body": "胴体", "lower_body": "骨盤・股",
    "arms": "腕・手", "hands": "腕・手", "legs": "脚・足", "whole_body": "全体",
}
# 左右を書き分けられるのは**対になっている肢**だけ。whole_body / head の pose に
# 「左右」を出すと選べてしまい、意味の無い観測が書ける (0010 の laterality の意図に反する)。
POSE_LATERAL_SCOPE = {"arms", "hands", "legs"}
# 下書きの置き場。**検証用インスタンスは KURA1_ANNOTATE_DIR で必ず分ける** —
# 同じディレクトリを指すと、テストの書き込みと本人の編集が同じ下書きを奪い合う
# (2026-07-28 実害: テストが編集中の draft を上書きした)。
ANN_DIR = pathlib.Path(__import__("os").environ.get(
    "KURA1_ANNOTATE_DIR", str(pathlib.Path.home() / "kura1" / "annotations")))
DATA_IMAGES = pathlib.Path(os.environ.get(
    "KURA_DATA_IMAGES",
    pathlib.Path(__file__).resolve().parent.parent / "data-images" / "posts"))


def build_id() -> str:
    """このサーバが動かしている**コードの指紋**。

    「リロードしても直らない、ハードリロードなら直る」という報告
    (user 2026-07-31) は、**画面が古い版かどうかを誰も見られない**ことが本体だった。
    HTML は no-store なので普通は起きないが、起きたときに**推測で片付けない**ために
    指紋を出す。画面は自分の指紋とサーバの指紋を比べ、違えば赤帯を出す。
    """
    import hashlib
    h = hashlib.sha1(pathlib.Path(__file__).read_bytes()).hexdigest()[:12]
    return h


BUILD = build_id()
# kura 本体への post リンクの origin (例 https://<canonical-host>:8443)。未設定ならリンクを出さない。
KURA_ORIGIN = json.dumps(os.environ.get("KURA_ORIGIN", "").rstrip("/"))
PORT = 8098
# 表示用に縮小した画像の置き場。**原本は絶対に触らない** — data-images は kura 本体の
# 資産で、注釈 UI の都合で書き換える対象ではない。
THUMB_DIR = pathlib.Path(os.environ.get(
    "KURA1_THUMB_DIR", str(pathlib.Path.home() / "kura1" / "thumbs")))


STRATA_TSV = pathlib.Path(__file__).resolve().parent / "probe" / "scene-strata.tsv"
# 層の並び順。**D は utility の分子に入らない**ので最後に置く (先に効く scene から埋める)。
STRATUM_ORDER = ["A", "B", "C", "E", "D"]
STRATUM_LABEL = {"A": "層A 効く場所", "B": "層B 複数人", "C": "層C control",
                 "E": "層E 材料", "D": "層D 外れ値"}


_strata_cache: dict = {}


def thumb(src: pathlib.Path, w: int) -> tuple[bytes, str]:
    """長辺 w px に縮めた WebP を返す。**原本より大きくはしない。**

    なぜ要るか (2026-07-30 実測): server も網も速い — HTML 1ms、4.45 MB の画像を
    別ノードから 0.16s。遅いのは**ブラウザ側の原寸デコード**で、`/fill` は
    72 枚を全部原寸で読んでいた (合計で数百 MB)。表示は数百 px なので、
    その画素を送る理由が無い。

    **注釈の判断に使う画面には縮小を使わない。** 乳輪の広さや precum のような
    細部は原寸でしか判らない。
    縮小するのは一覧のサムネイルと、原寸をすぐ開ける主画面だけ。

    生成物はディスクに置いて使い回す。key に原本の mtime を含めるので、
    原本が差し替われば別ファイルになる (古い縮小が残り続けない)。
    """
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    key = THUMB_DIR / f"{src.stem}-{int(src.stat().st_mtime)}-w{w}.webp"
    if not key.exists():
        from PIL import Image
        im = Image.open(src)
        im = im.convert("RGB") if im.mode not in ("RGB", "L") else im
        if max(im.size) > w:
            im.thumbnail((w, w), Image.LANCZOS)
        im.save(key, "WEBP", quality=88, method=4)
    return key.read_bytes(), "image/webp"


# ── pool を渡して作業対象を絞る (user 指示 2026-07-31。migration ではない) ──
# それまで作業対象は「kura1 に import 済の scene 全部」だったので、ラウンドが増えるほど
# 一覧に無関係な scene が混ざった。**pool を渡せばそのぶんだけ**にできる。
# 併せて未 import の pool は開いた時点で skeleton を作るので、
# import_probe_skeleton.py を手で回す手順が消える。
KURA_API = "http://127.0.0.1:8001"


def pool_posts(pool_id: int) -> list[int]:
    # production kura の pool メンバー。**読み取りのみ。**
    import json as _json, urllib.parse as _up, urllib.request as _ur
    out: list[int] = []
    off = 0
    while True:
        url = f"{KURA_API}/api/posts?" + _up.urlencode(
            {"query": f"pool:{pool_id}", "limit": 100, "offset": off})
        with _ur.urlopen(url, timeout=60) as r:
            d = _json.loads(r.read())
        page = [x["id"] for x in (d.get("results") or [])]
        out += page
        off += len(page)
        if not page or off >= d.get("total", 0):
            return out


def ensure_imported(pool_id: int) -> dict:
    # pool のうち kura1 に無い post を skeleton import する。**冪等**。
    # import_probe_skeleton.py を subprocess で呼ぶ — ロジックを二重に持たない
    # (0044 で「同じ表が 2 か所に住む」形を潰したのと同じ判断)。
    import subprocess
    ids = pool_posts(pool_id)
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        cur.execute("SELECT external_id FROM asset_source WHERE source_slug='kura_post'")
        have = {r[0] for r in cur.fetchall()}
    missing = [i for i in ids if str(i) not in have]
    if not missing:
        return {"pool": pool_id, "posts": len(ids), "imported": 0}
    r = subprocess.run(
        ["python3", str(pathlib.Path(__file__).resolve().parent / "import_probe_skeleton.py"),
         "--pool", str(pool_id)],
        capture_output=True, text=True, timeout=600)
    return {"pool": pool_id, "posts": len(ids), "imported": len(missing),
            "log": (r.stdout or r.stderr or "").strip().splitlines()[-2:]}


def strata() -> dict[int, str]:
    """post_id → 層。S-1 の選定結果 (broker fact) を repo 内の表に落としたもの。

    **注釈者に層を見せるのは S-1 の選定規則を教えることになる**ので、UI では層を
    「どの順で埋めるか」にしか使わない (query も期待結果も渡さない方針は S-8 のまま)。
    """
    out: dict[int, str] = {}
    if not STRATA_TSV.exists():
        return out
    # mtime が同じなら読み直さない (worklist のたびに 58 行を parse していた)
    mt = STRATA_TSV.stat().st_mtime
    if _strata_cache.get("mt") == mt:
        return _strata_cache["data"]
    for line in STRATA_TSV.read_text().splitlines():
        if line.startswith("#") or line.startswith("post_id"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].isdigit():
            out[int(parts[0])] = parts[1]
    _strata_cache.update(mt=mt, data=out)
    return out


_ARMS_CACHE = None


def _round4_arms() -> dict:
    """round 4 の arm 割り当て (post_id -> 'H'/'M')。生成規則は fact 6eadd66f。"""
    global _ARMS_CACHE
    if _ARMS_CACHE is None:
        f = pathlib.Path(__file__).with_name("probe") / "round4-arms.json"
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            _ARMS_CACHE = {int(k): v for k, v in d.get("arm", {}).items()}
        except Exception:
            _ARMS_CACHE = {}
    return _ARMS_CACHE


def worklist(pool_id: int | None = None) -> list[dict]:
    """**kura1 に入っている scene 全部**が作業対象 (DB が SoT)。

    以前はテンプレート JSON が置かれている post だけを対象にしていたので、
    最初のバッチ 8 件しか画面に出ず「続きがやれない」状態になっていた (2026-07-28)。
    下書きファイルは状態であって対象の定義ではない — 対象は DB に import 済の scene。

    1 件ずつ問い合わせると 58 回往復するので **1 本の集約 query** で確定数まで取る。
    """
    st = strata()
    arm_map = _round4_arms()
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        # **撤回された観測は数えない。** 生の行数で数えていた版は、全部撤回された
        # scene を「注釈済」と表示し、**注釈者にその scene を飛ばさせる**
        # (2026-07-30、0040 の動作確認を誤って第 2 ラウンドの scene に書いて
        #  撤回したときに発覚)。数えるのは「生きている payload」= 撤回でなく、
        # かつ自分を撤回する観測が存在しないもの。
        cur.execute(
            "SELECT s.external_id::int AS post_id,"
            "       count(o.id) AS written"
            "  FROM asset_source s"
            "  JOIN asset a ON a.id = s.asset_id"
            "  LEFT JOIN observation o ON o.scene_id = a.scene_id"
            "       AND o.retracts_id IS NULL"
            "       AND NOT EXISTS (SELECT 1 FROM observation r"
            "                        WHERE r.retracts_id = o.id)"
            " WHERE s.source_slug = 'kura_post'"
            " GROUP BY 1")
        written = {r[0]: r[1] for r in cur.fetchall()}
        # producer 別の生存所見数 (model / human を色分けするため)
        cur.execute(
            "SELECT s.external_id::int, o.producer_kind, count(o.id)"
            "  FROM asset_source s JOIN asset a ON a.id=s.asset_id"
            "  JOIN observation o ON o.scene_id=a.scene_id"
            "       AND o.retracts_id IS NULL"
            "       AND NOT EXISTS (SELECT 1 FROM observation r WHERE r.retracts_id=o.id)"
            " WHERE s.source_slug='kura_post' GROUP BY 1,2")
        byprod = {}
        for pid, pk, n in cur.fetchall():
            byprod.setdefault(pid, {}).update({pk: n})
    # pool を渡されたらそのメンバーだけに絞る。**DB 側で絞らない** — pool は
    # production kura の概念で、kura1 は post id しか知らない。
    keep = set(pool_posts(pool_id)) if pool_id else None
    rows = []
    for pid, n in written.items():
        if keep is not None and pid not in keep:
            continue
        f = ANN_DIR / f"{pid}.json"
        pr = byprod.get(pid, {})
        rows.append({"post_id": pid, "written": n, "saved": f.exists(),
                     "stratum": st.get(pid, "?"),
                     "arm": arm_map.get(pid),
                     "n_model": pr.get("model", 0), "n_human": pr.get("human", 0),
                     "state": "done" if n else ("draft" if f.exists() else "todo")})
    rows.sort(key=lambda r: (STRATUM_ORDER.index(r["stratum"])
                             if r["stratum"] in STRATUM_ORDER else 99, r["post_id"]))
    return rows


# 語彙は ontology 表からしか変わらない (migration を当てたとき)。毎回 12 本ほど query を
# 投げ直す必要はないので、**ontology 表への書き込み量**を版として持ち回りキャッシュする。
# 観測を足しても版は変わらない (観測は ontology ではない) ので、注釈中に無駄な再構築は
# 起きない。統計がリセットされても版が変わるだけで、余分に 1 回作り直すだけで済む。
_ONTOLOGY_TABLES = (
    "body_region", "region_class_map", "attribute", "attribute_value", "pose",
    "pose_category", "relation", "relation_applicability", "scene_descriptor",
    "scene_facet", "species", "character", "gender_presentation", "object_type",
    "object_state", "observation_domain", "asset_region_anchor", "unit",
    # 0046/0052 で足した表。**ここに入れ忘れると migration しても vocab が古いまま**に
    # なり、再起動するまで新しい語が画面に出ない (2026-07-31 に気付いた潜在バグ)
    "object_variant", "scene_surface",
)
_vocab_cache: dict = {}


def ontology_version(cur) -> str:
    cur.execute(
        "SELECT coalesce(sum(n_tup_ins + n_tup_upd + n_tup_del),0)"
        "  FROM pg_stat_user_tables WHERE schemaname='kura' AND relname = ANY(%s)",
        (list(_ONTOLOGY_TABLES),))
    return str(cur.fetchone()[0])


def vocab() -> dict:
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        ver = ontology_version(cur)
    if _vocab_cache.get("ver") == ver:
        return _vocab_cache["data"]
    data = _vocab_build()
    _vocab_cache.update(ver=ver, data=data)
    return data


def _vocab_build() -> dict:
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        q = lambda s: [r[0] for r in cur.execute(s).fetchall()]  # noqa: E731
        # description は 0006 で入れた。**語を選べないのは ontology の欠陥**なので UI に出す。
        # body_region は意図的に未記述 (解剖学名は prose より構造の方が情報量が多い) →
        # parent 連鎖 + laterality + class を代わりに見せる。
        def withdesc(sql):
            return [{"slug": r[0], "d": r[1] or ""} for r in cur.execute(sql).fetchall()]
        # 0011: 値にも説明が付いた。属性を選べても値を選べないと意味がないので UI に出す。
        vals: dict[str, list[dict]] = {}
        for a, v, d in cur.execute(
                "SELECT attribute_slug, value_slug, description FROM attribute_value"
                " ORDER BY attribute_slug, display_order, value_slug").fetchall():
            vals.setdefault(a, []).append({"slug": v, "d": d or ""})
        # 0016: is_multivalued=true の属性は UI がチェックボックスで出す
        # (どれが多値かは ontology が持つ — UI に slug を直書きしない)
        attrs = [{"slug": s, "value_type": vt, "applies_kind": ak, "applies_key": akey,
                  "values": vals.get(s, []), "d": d or "", "multi": multi}
                 for s, vt, ak, akey, d, multi in cur.execute(
                     # 0029: 表示順は attribute.display_order。0 = 未指定なので末尾へ。
                     # slug 順のままだと hair_color が hair_density より前に来る。
                     "SELECT slug, value_type, applies_kind, applies_key, description,"
                     " is_multivalued FROM attribute"
                     " ORDER BY (display_order = 0), display_order, slug").fetchall()]
        return {
            "presentation": q("SELECT slug FROM gender_presentation ORDER BY slug"),
            # **display_name も送る。** slug だけ送っていた版では日本語で絞り込めず、
            # 「ミノタウロスが選択できない」と報告された (2026-07-30)。
            # 0040/0041 で入れた種は display_name が日本語なので、それが届かないと
            # 「ミノタ」で 0 件になる。n = 表示名 (opt() の nm() が使う)。
            # **非推奨の語は選択肢から外す。** 0059 で horse_girl を非推奨にしたのに
            # 一覧に残していたため、注釈者が選び続け、**確定が毎回検証で落ちていた**
            # (user 指摘 2026-07-31「確定して次へで画像が切り替わらない」の実体)。
            # 既に書いてある値は表示のために別途候補へ足される (opt() の作り)。
            "species": [{"slug": r[0], "n": (r[1] or r[0])} for r in cur.execute(
                "SELECT slug, display_name FROM species"
                " WHERE display_name NOT LIKE '%[非推奨]%'"
                " ORDER BY slug").fetchall()],
            # 0007 で 117 語すべてに日本語説明を入れた。構造 (parent/laterality/class) は
            # 補助として末尾に添える — 説明が主で構造が従。
            "region": region_rows(cur),
            # pose は scope でグループ化 (全体 / 顔・頭 / 腕 / 手 / 脚)。
            # 性交時の構えは category=sexual_configuration なので独立グループに出す。
            "pose": sorted(
                [{"slug": r[0], "d": r[3] or "",
                  "g": ("性交時の構え" if r[2] == "sexual_configuration"
                        else POSE_GROUP.get(r[1], r[1])),
                  # rg = どの部位カテゴリの下に出すか、lat = 左右を書き分けられるか
                  "rg": POSE_REGION_GROUP.get(r[1], "全体"),
                  "lat": r[1] in POSE_LATERAL_SCOPE} for r in cur.execute(
                    "SELECT slug, scope, category, description FROM pose"
                    " ORDER BY slug").fetchall()],
                key=lambda x: (POSE_ORDER.index(x["g"]) if x["g"] in POSE_ORDER else 99,
                               x["slug"])),
            "relation": [{"slug": r[0], "d": f"[{r[1]}/{r[2]}] {r[3] or ''}"} for r in cur.execute(
                "SELECT slug, category, directionality, description FROM relation"
                " ORDER BY category, slug").fetchall()],
            # 47 語を平坦に並べず facet で分ける (2026-07-28 指摘)。分類軸は
            # scene_facet に既にある — 0018 で足したのは短い日本語名と並び順だけ。
            "descriptor": [{"slug": r[0], "d": r[1] or "", "g": r[2] or r[3]}
                           for r in cur.execute(
                "SELECT d.slug, d.description, f.display_name, d.facet"
                " FROM scene_descriptor d JOIN scene_facet f ON f.slug = d.facet"
                # 0019/0020: 機械で決まる語は**選択肢として出さない**。facet 単位
                # (orientation) と descriptor 単位 (色モード) の両方を見る。UI に
                # slug を直書きせず、宣言を読んで除く。
                " WHERE f.derived_from IS NULL AND d.derived_from IS NULL"
                " ORDER BY f.display_order, f.slug, d.slug").fetchall()],
            "object": q("SELECT slug FROM object_type ORDER BY slug"),
            "object_state": q("SELECT slug FROM object_state ORDER BY slug"),
            # 0046: 形。**部位とは独立** — flat tag は nipple_rings のように
            # 「部位 × 形」の合成語で持つが、ここは軸として分ける
            # 0052: 関係の対象になりうる環境 (地面・空中など)
            "surface": [{"slug": r[0], "n": r[1], "d": r[2] or ""} for r in cur.execute(
                "SELECT slug, display_name, description FROM scene_surface"
                " ORDER BY slug").fetchall()],
            "object_variant": [{"slug": r[0], "n": r[1], "d": r[2] or ""} for r in cur.execute(
                "SELECT slug, display_name, description FROM object_variant"
                " ORDER BY slug").fetchall()],
            "domain": q("SELECT slug FROM observation_domain ORDER BY slug"),
            # 0030: 人 / 非人間の動作主 / 物。触手の主は person でも object でもない
            "entity_kind": [{"slug": r[0], "d": r[1] or ""} for r in cur.execute(
                "SELECT slug, description FROM entity_kind ORDER BY"
                " (slug='person') DESC, slug").fetchall()],

            "attribute": attrs,
            **region_attr_map(cur, attrs),
        }


def region_attr_map(cur, attrs: list[dict]) -> dict:
    """部位 → その部位に付く属性、および部位に紐づかない属性 を返す。

    「breast の属性が『見えている部位』の breast と離れた場所にあって選びにくい」
    (2026-07-28 指摘) への対応。属性を部位の下に置くには、どの属性がどの部位に
    付くかを UI が知っている必要がある。

    規則:
      * applies_kind='region' → その region だけ (例 breast_size → breast)
      * applies_kind='class'  → そのクラスを持つ region 全部 (例 skin_tone → surface_skin)
      * applies_kind='any_region' → **どの region にも付く** (0065)。
        以前は `ANY_REGION = {pigmentation, surface_substance}` という Python の定数で
        2 つだけ特別扱いしていたが、**例外が機構の外にある**形だった
        (DB を読んでも判らず、3 つ目を足す人が壊せる)。表の値に移した。
      * それ以外の applies_kind='entity' → 部位に紐づかない (人物全体の属性)
    """
    cur.execute("SELECT slug, primary_class FROM body_region")
    prim = dict(cur.fetchall())
    cur.execute("SELECT region_slug, class_slug FROM region_class_map")
    classes: dict[str, set] = {r: {c} for r, c in prim.items()}
    for r, c in cur.fetchall():
        classes.setdefault(r, set()).add(c)

    by_region: dict[str, list] = {r: [] for r in prim}
    entity_level: list[str] = []
    for a in attrs:
        slug, kind, key = a["slug"], a["applies_kind"], a["applies_key"]
        if kind == "any_region":
            for r in by_region:
                by_region[r].append(slug)
        elif kind == "region":
            if key in by_region:
                by_region[key].append(slug)
            else:
                entity_level.append(slug)
        elif kind == "class":
            # applies_key は**空白区切りで複数 class** を書ける (0085 —
            # pigmentation = surface_skin + external_genital + surface_mucosa + perianal)
            keys = set(key.split())
            hit = [r for r, cs in classes.items() if keys & cs]
            for r in hit:
                by_region[r].append(slug)
            if not hit:
                entity_level.append(slug)
        else:
            entity_level.append(slug)
    return {"region_attrs": by_region, "entity_attrs": entity_level,
            # entity 属性のうち、表示だけ特定の部位カテゴリの下に置くもの
            "entity_attr_group": {a: g for a, g in ENTITY_ATTR_GROUP.items()
                                  if a in entity_level},
            "emission_attrs": [a for a in EMISSION_ATTRS if a in entity_level],
            "emission_rels": EMISSION_RELS,
            "reject_reasons": [{"code": r[0], "n": r[1], "w": r[2]} for r in cur.execute(
                "SELECT code, display_name, training_weight FROM model_rejection_reason"
                " ORDER BY (training_weight='strong') DESC, code").fetchall()],
            # 0070: **自由入力を許す属性は DB の CHECK から読む** — UI に slug を
            # 書くと 0065 で消したハードコードの例外がまた生える。
            # CHECK を緩めれば UI も自動で追随する
            "text_attrs": text_attrs(cur),
            "macros": macro_rows(cur)}


def kura_pool_info(pool_id: int) -> tuple[str, list[int]]:
    """kura 本番 DB (read-only) から pool 名と post 一覧を引く。

    pool は「一括判定する対象集合」の確立された器 (検証プール 8009/8010) —
    画像確認とマーキングを別画面に分けない (user 指摘 2026-08-01) ための
    オブザベーションビュー (/pool/<id>) がここを読む。書き込みは一切しない。
    """
    env: dict = {}
    for line in open(pathlib.Path(__file__).resolve().parent.parent / "api" / "env"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    dsn = env["DATABASE_URL"].replace("+asyncpg", "")
    with psycopg.connect(dsn) as c, c.cursor() as cur:
        cur.execute("SELECT name FROM pool_name WHERE pool_id=%s ORDER BY ord LIMIT 1",
                    (pool_id,))
        row = cur.fetchone()
        cur.execute("SELECT post_id FROM pool_post WHERE pool_id=%s ORDER BY ord",
                    (pool_id,))
        return (row[0] if row else f"pool {pool_id}"), [r[0] for r in cur.fetchall()]


def pool_observations(pool_id: int, attr: str, region: str) -> dict:
    """pool の各 post について、人物ごとの attr の human/model 値をまとめる。"""
    name, posts = kura_pool_info(pool_id)
    V = vocab()
    meta = next((a for a in V["attribute"] if a["slug"] == attr), None)
    vals = [v["slug"] for v in (meta.get("values") or [])] if meta else []
    out = {"pool": pool_id, "name": name, "attr": attr, "region": region,
           "values": vals, "posts": []}
    prefix = attr + "="
    for pid in posts:
        d = scene_observations(pid)
        refs: dict = {}
        for o in d["human"]:
            if o["axis"] in ("species", "presentation") and o["ref"]:
                refs.setdefault(o["ref"], {"human": None, "model": None})
        for side in ("human", "model"):
            for o in d[side]:
                if (o["axis"] == "attribute" and o["val"].startswith(prefix)
                        and (o.get("region") or "") == region and o["ref"] in refs):
                    refs[o["ref"]][side] = o["val"].split("=", 1)[1]
        out["posts"].append({"post": pid,
                             "refs": [{"ref": r, **v} for r, v in sorted(refs.items())]})
    return out


def pool_anchor_data(pool_id: int) -> dict:
    """pool の各 post について、人物ごとの anchor (human 実値 / model ヒント) を返す。

    位置版オブザベーションビュー (/pool/<id>?mode=anchor) 用。合否採点の分母は
    「human anchor が全員に付いた複数人 scene」なので (fact 30fab4d1)、ここで
    付ければ scene が判定に入る。
    """
    name, posts = kura_pool_info(pool_id)
    out = {"pool": pool_id, "name": name, "posts": []}
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        for pid in posts:
            cur.execute("""
                SELECT o.producer_kind, o.subject_ref, ar.anchor_x, ar.anchor_y, o.id
                  FROM observation o
                  JOIN asset a ON a.scene_id = o.scene_id
                  JOIN asset_source s ON s.asset_id = a.id AND s.source_slug='kura_post'
                  LEFT JOIN asset_region ar ON ar.id = o.subject_asset_region_id
                             AND ar.anchor_x IS NOT NULL
                 WHERE s.external_id = %s AND o.subject_ref IS NOT NULL
                   AND o.retracts_id IS NULL
                   AND NOT EXISTS (SELECT 1 FROM observation r WHERE r.retracts_id=o.id)
                 ORDER BY o.id""", (str(pid),))
            refs: dict = {}
            for pk, ref, x, y, _oid in cur.fetchall():
                if pk == "human":
                    e = refs.setdefault(ref, {"human": None, "model": None})
                    if x is not None:
                        e["human"] = {"x": float(x), "y": float(y)}
                elif pk == "model" and ref in refs:
                    if x is not None:
                        refs[ref]["model"] = {"x": float(x), "y": float(y)}
            # model だけが見た ref はボタンにしない (human の人物列挙が SoT)
            out["posts"].append({"post": pid,
                                 "refs": [{"ref": r, **v} for r, v in sorted(refs.items())]})
    return out


def scene_observations(post_id: int) -> dict:
    """その scene の**生きている観測**を producer 別に読む (0079 後の色分け表示用)。

    UI の下書き (human 作業中) とは別に、**DB に確定済みの観測**を出す。
    P0 以降は model 名義の観測がここに出て、人間はそれを見て撤回/追加する。
    人間が確定した所見も同じ形で並べ、**producer で色分け**する。
    """
    rows = []
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        cur.execute(
            "SELECT o.id, o.producer_kind, o.producer_ref, o.domain, o.subject_ref,"
            "       oi.species_slug, oi.presentation, br.region_slug, op.pose_slug,"
            "       oa.attribute_slug, oa.value_slug, oa.region_slug,"
            "       orl.relation_slug, orl.subject_ref, orl.subject_region_slug,"
            "       orl.object_ref, orl.object_region_slug, osd.descriptor_slug"
            "  FROM observation o"
            "  JOIN asset a ON a.scene_id=o.scene_id"
            "  JOIN asset_source s ON s.asset_id=a.id"
            "  LEFT JOIN obs_identity oi ON oi.observation_id=o.id"
            "  LEFT JOIN obs_body_region br ON br.observation_id=o.id"
            "  LEFT JOIN obs_pose op ON op.observation_id=o.id"
            "  LEFT JOIN obs_attribute oa ON oa.observation_id=o.id"
            "  LEFT JOIN obs_relation orl ON orl.observation_id=o.id"
            "  LEFT JOIN obs_scene_descriptor osd ON osd.observation_id=o.id"
            " WHERE s.source_slug='kura_post' AND s.external_id=%s"
            "   AND o.retracts_id IS NULL"
            "   AND NOT EXISTS (SELECT 1 FROM observation r WHERE r.retracts_id=o.id)"
            " ORDER BY o.producer_kind, o.id", (str(post_id),))
        for (oid, pk, pref, dom, ref, sp, pres, reg, pose, aslug, aval, areg,
             rel, rsub, rsubr, robj, robjr, desc) in cur.fetchall():
            # identity は species と presentation を**同じ観測行**に持ち得る。
            # elif で一方に倒すと presentation が UI から丸ごと消える
            # (2026-07-31、突合チップのコントロール統合で発覚した既存欠陥)
            if sp or pres:
                for axis, v, label in ((("species", sp, f"種族 {ref}: {sp}"),)
                                       if sp else ()) + \
                                      ((("presentation", pres, f"性別 {ref}: {pres}"),)
                                       if pres else ()):
                    rows.append({"id": oid, "producer": pk, "producer_ref": pref,
                                 "domain": dom, "label": label,
                                 "ref": ref, "axis": axis, "val": v, "region": None})
                continue
            if reg:     label = f"部位 {ref}: {reg}"
            elif pose:  label = f"ポーズ {ref}: {pose}"
            elif aslug: label = f"属性 {ref}: {aslug}={aval or '-'}" + (f" @{areg}" if areg else "")
            elif rel:   label = f"関係 {rsub or ref}/{rsubr or '-'} →{rel}→ {robj or '-'}/{robjr or '-'}"
            elif desc:  label = f"画面 {desc}"
            else:       label = f"{dom} {ref or ''}"
            # 構造化: 右パネルで ref・軸・値で突き合わせるため
            val = (sp or pres or reg or pose or
                   (f"{aslug}={aval or '-'}" if aslug else None) or
                   (rel and f"{rsub or ref}/{rsubr or '-'}>{rel}>{robj or '-'}/{robjr or '-'}")
                   or desc)
            axis = ("species" if sp else "presentation" if pres else "region" if reg
                    else "pose" if pose else "attribute" if aslug else "relation" if rel
                    else "descriptor" if desc else dom)
            rows.append({"id": oid, "producer": pk, "producer_ref": pref,
                         "domain": dom, "label": label,
                         "ref": ref, "axis": axis, "val": val,
                         "region": areg or reg})
    # model は**最新世代 (producer_ref) だけ**見せる (2026-08-01)。判定基準を直して
    # 再走した世代が最新である以上、旧世代の誤り (state 散布等) をチェック UI に
    # 混ぜて ✗ を強いる理由が無い。旧世代は履歴として DB に残る (append-only)。
    # 機械の一括撤回はしない — retract は producer='human' 固定で、機械撤回は
    # 出所の洗浄 (P4 の教師データ汚染) になる。
    model_rows = [r for r in rows if r["producer"] == "model"]
    if model_rows:
        # 「最新世代」の SoT は observer_version の登録順 (0086) — max(観測id) の
        # ヒューリスティックは 1 scene だけの先行テストで scene ごとに世代が
        # バラつくので廃止。未登録 ref は劣後、同順位は観測 id で比べる
        with psycopg.connect(DSN) as c2, c2.cursor() as cur2:
            cur2.execute("SET search_path = kura, public")
            cur2.execute("SELECT producer_ref, id FROM observer_version")
            gen_order = dict(cur2.fetchall())
        latest = max(model_rows, key=lambda r: (gen_order.get(r["producer_ref"], -1),
                                                r["id"]))["producer_ref"]
        model_rows = [r for r in model_rows if r["producer_ref"] == latest]
    return {"post_id": post_id,
            "model": model_rows,
            "human": [r for r in rows if r["producer"] == "human"]}


def pattern_hits(slug: str) -> dict:
    """型に当たる post を返す (0072)。照合の実体は find_pattern.py に置いてある —
    **同じ規則を 2 か所に書かない** (片方だけ直したときに差が出る)。
    """
    import subprocess
    r = subprocess.run(
        ["python3", str(pathlib.Path(__file__).with_name("find_pattern.py")),
         "--macro", slug],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return {"error": (r.stderr or "").strip()[:300]}
    line = (r.stdout or "").strip().splitlines()
    if not line:
        return {"posts": []}
    parts = line[0].split()
    posts = [x for x in parts if x.isdigit() and len(x) >= 4]
    n = 0
    for i, x in enumerate(parts):
        if x == "件" and i and parts[i - 1].isdigit():
            n = int(parts[i - 1])
    return {"posts": posts, "n": n}


def text_attrs(cur) -> list[str]:
    """自由入力 (text_content) を許す属性を **DB の CHECK から読む** (0070)。

    UI に slug を書くと、0065 で消したばかりのハードコードの例外がまた生える。
    CHECK が唯一の権威で、緩めれば UI も自動で追随する。
    """
    cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conrelid='obs_attribute'::regclass AND contype='c'"
                "   AND pg_get_constraintdef(oid) LIKE '%text_content%'")
    import re as _re
    out: list[str] = []
    for (d,) in cur.fetchall():
        out += _re.findall(r"attribute_slug = '([a-z_]+)'", d)
    return sorted(set(out))


def macro_rows(cur) -> list[dict]:
    """一文で複数のコントロールを埋める **人が書いた展開の型** (0064)。

    推測ではないので、展開先をそのまま画面に渡す — 何が入るかを注釈者が読める。
    展開は 3 表に正規化してある (ADR-0002。jsonb 1 列だと FK が効かず、
    存在しない関係や部位を指す型が入ってしまう) ので、ここで組み立て直す。
    """
    cur.execute("SELECT slug, phrase, aliases, description, tag_name"
                " FROM phrase_macro ORDER BY display_order, slug")
    out = {s: {"slug": s, "phrase": ph, "alias": list(al or []), "d": d or "",
               "tag": tg, "x": {"relations": [], "poses": {}}}
           for s, ph, al, d, tg in cur.fetchall()}
    cur.execute(
        "SELECT macro_slug, relation_slug, subject_ref, subject_region, subject_laterality,"
        " object_ref, object_region, object_laterality, object_surface"
        " FROM phrase_macro_relation ORDER BY macro_slug, ord")
    for m, rel, sr, sg, sl, orf, og, ol, osf in cur.fetchall():
        def side(ref, region, lat):
            d = {}
            if ref:
                d["ref"] = ref
            if region:
                d["region"] = region
            if lat:
                d["laterality"] = lat
            return d
        r = {"relation": rel, "subject": side(sr, sg, sl), "object": side(orf, og, ol)}
        if osf:
            r["object"]["surface"] = osf
        out[m]["x"]["relations"].append(r)
    cur.execute("SELECT macro_slug, subject_ref, pose_slug FROM phrase_macro_pose"
                " ORDER BY macro_slug, subject_ref, pose_slug")
    for m, ref, pz in cur.fetchall():
        out[m]["x"]["poses"].setdefault(ref, []).append(pz)
    return list(out.values())


def region_rows(cur) -> list[dict]:
    """部位を **body の直下の祖先** でグループ化して返す (頭部 / 胴体 / 腕・手 …)。

    117 語を平坦に並べると探せない。再帰 CTE で最上位まで遡ると全部 'body' に
    潰れてしまったので (2026-07-28 実害)、Python 側で明示的に 1 段ずつ辿る。
    """
    cur.execute("SELECT slug, parent_slug, description,"
                " nullif(concat_ws(' / ', parent_slug, laterality, primary_class),''),"
                " laterality"
                " FROM body_region ORDER BY slug")
    rows = cur.fetchall()
    parent = {r[0]: r[1] for r in rows}

    def top(slug: str) -> str:
        seen = set()
        cur_ = slug
        while parent.get(cur_) and parent[cur_] != "body" and cur_ not in seen:
            seen.add(cur_)
            cur_ = parent[cur_]
        return cur_ if parent.get(cur_) == "body" else (cur_ if cur_ != slug else slug)

    out = []
    for slug, par, desc, struct, lat in rows:
        g = ("全体" if par is None
             else REGION_OVERRIDE.get(slug)
             or REGION_GROUP.get(top(slug), "その他"))
        # bilateral な部位だけ「左右」を出す。midline (navel / mons / anus …) に
        # 左右を出すと、意味の無い値を選べてしまう。
        out.append({"slug": slug, "d": (desc or "") + (f"  [{struct}]" if struct else ""),
                    "g": g, "lat": lat == "bilateral"})
    out.sort(key=lambda x: (REGION_ORDER.index(x["g"]) if x["g"] in REGION_ORDER else 99,
                            x["slug"]))
    return out


_derived_cache: dict = {}


_img_cache: dict = {}


def warm_img_cache() -> int:
    """全 post の画像パスを**1 本の query で**先読みする。

    `image_path()` は 1 枚ごとに `psycopg.connect` していたので、サムネイルを 76 枚
    並べた初回表示で**同時に 76 接続**を張っていた (user 報告 2026-07-31
    「リロードすると画像が表示されない。ハードリロードすると治る」— 2 回目は
    cache が温まっていて接続が要らないので直るように見えていた)。
    """
    n = 0
    try:
        with psycopg.connect(DSN) as c, c.cursor() as cur:
            cur.execute("SET search_path = kura, public")
            cur.execute("SELECT s.external_id::int, b.external_ref FROM blob b"
                        " JOIN asset a ON a.blob_id=b.id"
                        " JOIN asset_source s ON s.asset_id=a.id"
                        " WHERE s.source_slug='kura_post'")
            for pid, ref in cur.fetchall():
                p = pathlib.Path(ref)
                if pid not in _img_cache and p.exists():
                    _img_cache[pid] = [p]
                    n += 1
    except Exception:                                   # noqa: BLE001 起動を妨げない
        pass
    return n


def image_path(post_id: int) -> list[pathlib.Path]:
    """post の画像パス。**DB の blob.external_ref を引く。**

    以前は `data-images/posts` を `glob("<pid>_*")` していたが、このディレクトリには
    **773,578 ファイル**あり 1 回あたり 0.49 秒かかっていた (2026-07-29 実測)。
    リロードのたびに画像 1 枚につき 0.5 秒を捨てていたことになる。
    blob は取り込み時に実体のパスを持っているので、ディレクトリを走査する理由が無い。
    DB にも無い場合だけ glob に落ちる (取り込み前の post を開いたとき)。
    """
    if post_id in _img_cache:
        return _img_cache[post_id]
    hits: list[pathlib.Path] = []
    try:
        with psycopg.connect(DSN) as c, c.cursor() as cur:
            cur.execute("SET search_path = kura, public")
            cur.execute("SELECT b.external_ref FROM blob b JOIN asset a ON a.blob_id=b.id"
                        " JOIN asset_source s ON s.asset_id=a.id"
                        " WHERE s.source_slug='kura_post' AND s.external_id=%s", (str(post_id),))
            r = cur.fetchone()
        if r and pathlib.Path(r[0]).exists():
            hits = [pathlib.Path(r[0])]
    except Exception:                                   # noqa: BLE001 DB 不調でも画像は出す
        hits = []
    if not hits:
        hits = sorted(DATA_IMAGES.glob(f"{post_id}_*"))  # 取り込み前の post への保険
    if hits:
        _img_cache[post_id] = hits
    return hits


def fill_rows() -> list[dict]:
    """S-11 が要求するのに注釈されていない観測を、scene ごとに集めて返す。

    S-3 の分類表が「解ける」と判定した B2 (eye_color) / B4・B5 (entity_count) に
    **実データが 0 件**だった (2026-07-29 実測)。分類は「表現できるか」の判定で、
    「実際に注釈したか」は別 — ここが空だと副 gate の過半が測れない。

    `entity_count` は**注釈者が既に宣言した人物カードから導ける** (person_N の
    presentation を数えるだけ)。新しい判断は要らないので既定値として出す。
    ただし**自動で確定はしない** — 注釈していない背景人物がいれば実際の人数と
    ずれるので、確認は人がする。
    `eye_color` は見ないと決まらないので空で出す。
    """
    st = strata()
    arm_map = _round4_arms()
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        cur.execute(
            "SELECT s.external_id::int, count(*) FILTER (WHERE o.domain='entity_count'),"
            "       count(*) FILTER (WHERE a.attribute_slug='eye_color')"
            "  FROM asset_source s JOIN asset ast ON ast.id=s.asset_id"
            "  LEFT JOIN observation o ON o.scene_id=ast.scene_id AND o.event_type<>'retraction'"
            "  LEFT JOIN obs_attribute a ON a.observation_id=o.id"
            " WHERE s.source_slug='kura_post' GROUP BY 1")
        have = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    out = []
    for pid, (n_count, n_eye) in sorted(have.items()):
        f = ANN_DIR / f"{pid}.json"
        if not f.exists():
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        subs = [{"ref": s.get("ref"), "presentation": s.get("presentation") or "",
                 "kind": s.get("kind", "person"),
                 "eye": next((a.get("value") for a in (s.get("attributes") or [])
                              if a.get("attribute") == "eye_color"), ""),
                 "has_eye_region": "eye" in (s.get("regions") or [])}
                for s in d.get("subjects", [])]
        # 宣言済みの人物カードから人数を導く (person だけ数える)
        derived: dict = {}
        for s in subs:
            if s["kind"] == "person" and s["presentation"]:
                derived[s["presentation"]] = derived.get(s["presentation"], 0) + 1
        out.append({"post_id": pid, "stratum": st.get(pid, "?"),
                    "subjects": subs,
                    "derived_counts": [{"presentation": k, "count_min": v, "count_exact": True}
                                       for k, v in sorted(derived.items())],
                    "counts": d.get("entity_counts") or [],
                    "written_counts": n_count, "written_eye": n_eye})
    return out


def collect_gaps() -> list[dict]:
    """全下書きから「書けなかったこと」を集める。

    **穴はその場で塞がない** (user 判断 2026-07-29)。注釈中に設計を止めて考えると
    重すぎるので、気付いたことはここに溜めて後でまとめて設計する。まとめてやる方が
    軸の切り方 (触手を本数で数えるか等) も一貫する。

    観測ではないので DB には入れない — これは **ontology についてのメモ**であって
    scene についての事実ではない。下書き JSON に置く。
    """
    out = []
    for f in sorted(ANN_DIR.glob("*.json")):
        if not f.stem.isdigit():
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:                                   # noqa: BLE001
            continue
        g = (d.get("gaps") or "").strip()
        if g:
            out.append({"post_id": int(f.stem), "gaps": g})
    return out


def derived_facts(post_id: int) -> dict:
    """人手で書かない、**計算で決まる**事実。

    「画面の向きはアスペクト比から自動的に定まるのではないの？」(2026-07-28 指摘)。
    その通りなので注釈者には出さず、ここで導いて表示だけする。**保存はしない** —
    blob.width / blob.height があれば一意に決まるので、行にすると二重の真実になる。
    """
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        cur.execute("SELECT b.width, b.height, b.external_ref FROM blob b"
                    " JOIN asset a ON a.blob_id = b.id"
                    " JOIN asset_source s ON s.asset_id = a.id"
                    " WHERE s.source_slug='kura_post' AND s.external_id=%s", (str(post_id),))
        r = cur.fetchone()
    if not r or r[0] is None:
        return {}
    w, h, ref = r
    # 画像が変わらなければ測り直さない (色モードの測定は 1 枚あたり実測 50ms〜3s)
    try:
        key = (ref, pathlib.Path(ref).stat().st_mtime)
    except OSError:
        key = (ref, None)
    if _derived_cache.get(post_id, (None,))[0] == key:
        return _derived_cache[post_id][1]
    out = {"width": w, "height": h,
           "orientation": "landscape" if w > h else ("portrait" if w < h else "square")}
    # 色モードも画素から測れる (0020)。**決め切れなければ value=None** で、その場合は
    # 誰も書かない — 測れないのに断定しないため。理由は必ず添えて検算できるようにする。
    try:
        cm, why, _ = _derive.color_mode(pathlib.Path(ref))
        out["color_mode"], out["color_why"] = cm, why
    except Exception as e:                       # noqa: BLE001 画像が読めなくても UI は出す
        out["color_mode"], out["color_why"] = None, f"測定できない ({e})"
    _derived_cache[post_id] = (key, out)
    return out


# 検出器は torch を要るので専用 venv に隔離してある (ComfyUI の .venv を汚さない)。
# **提案であって観測ではない** — 返した内容は下書きに入るだけで、人が見て直すまで
# DB には行かない。機械の出力にも非捏造の原則を同じように効かせる。
DETECT_PY = os.environ.get(
    "KURA1_DETECT_PY", str(pathlib.Path.home() / "kura1-detect-venv" / "bin" / "python"))
PROPOSE = str(pathlib.Path(__file__).resolve().parent / "propose_subjects.py")


def propose_subjects(post_id: int) -> dict:
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        cur.execute("SELECT b.external_ref FROM blob b JOIN asset a ON a.blob_id=b.id"
                    " JOIN asset_source s ON s.asset_id=a.id"
                    " WHERE s.source_slug='kura_post' AND s.external_id=%s", (str(post_id),))
        r = cur.fetchone()
    if not r:
        return {"error": "画像が見つからない"}
    import subprocess
    try:
        out = subprocess.run([DETECT_PY, PROPOSE, r[0], "--json"],
                             capture_output=True, text=True, timeout=120)
    except Exception as e:                       # noqa: BLE001
        return {"error": f"検出器を実行できない: {e}"}
    line = next((x for x in out.stdout.splitlines() if x.startswith("{")), None)
    if not line:
        return {"error": f"検出器の出力を読めない: {out.stderr[-300:]}"}
    return json.loads(line)


def already_written(post_id: int) -> int:
    """この post に既に入っている観測数 (append-only なので消せない — 二重投入の警告用)。"""
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        cur.execute("SELECT count(*) FROM observation o JOIN asset a ON a.scene_id=o.scene_id"
                    " JOIN asset_source s ON s.asset_id=a.id"
                    " WHERE s.source_slug='kura_post' AND s.external_id=%s", (str(post_id),))
        return cur.fetchone()[0]


class Handler(BaseHTTPRequestHandler):
    # **HTTP/1.1 にして keep-alive を効かせる。** 既定の HTTP/1.0 は接続を毎回
    # 張り直すので、サムネイル 76 枚を並べた瞬間にブラウザの同時接続上限と
    # ぶつかり `Connection reset by peer` が出て**画像が欠ける / クリックが効かない**
    # (user 報告 2026-07-31)。_send は必ず Content-Length を出すので 1.1 で正しい。
    protocol_version = "HTTP/1.1"
    server_version = "kura1-annotate"

    # **書き込み系だけは記録する。** 「押しても進まない」の切り分けで
    # 「ブラウザが何を投げたか」を推測するしかない状態になった (2026-07-31)。
    # 画像や語彙の GET は量が多いので出さない。
    ACCESS_LOG = pathlib.Path(os.environ.get(
        "KURA1_ANNOTATE_LOG", "/tmp/kura1-annotate-access.log"))

    def log_message(self, fmt, *args):  # 画像や語彙の GET は静かに
        pass

    def note(self, what: str) -> None:
        try:
            with self.ACCESS_LOG.open("a", encoding="utf-8") as f:
                f.write(f"{datetime.datetime.now():%H:%M:%S} "
                        f"{self.client_address[0]} {what}\n")
        except Exception:
            pass

    def _send(self, code: int, body: bytes, ctype: str,
              cache: str = "no-store", etag: str | None = None) -> None:
        """`cache`/`etag` を渡さない応答は従来どおり no-store (下書き等は毎回取り直す)。

        **画像と語彙は no-store にしてはいけない。** リロードのたびに 4.9 MB の画像を
        落とし直していた (2026-07-29 実測)。内容が変わらないものに no-store を付けるのは
        「常に正しい」ではなく「常に遅い」だけ。
        """
        # If-None-Match が一致すれば本体を送らない (304)
        if etag and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache)
            self.end_headers()
            return
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        p = urllib.parse.urlparse(self.path).path
        try:
            if p == "/":
                self._send(200, PAGE.replace("__BUILD__", BUILD).replace("__KURA_ORIGIN__", KURA_ORIGIN).encode(),
                           "text/html; charset=utf-8")
            elif p == "/fill":
                self._send(200, FILL_PAGE.encode(), "text/html; charset=utf-8")
            elif p == "/api/fill":
                self._json(200, fill_rows())
            elif p == "/api/gaps":
                self._json(200, collect_gaps())
            elif p.startswith("/api/pool_import/"):
                # **開いた時点で skeleton を作る。** import_probe_skeleton.py を手で
                # 回す手順を無くすため (user 指示 2026-07-31「pool を与えれば」)。冪等。
                self._json(200, ensure_imported(int(p.rsplit("/", 1)[1])))
            elif p == "/api/worklist":
                warm_img_cache()      # サムネイル 76 枚が来る前に 1 本で解決しておく
                # **派生計測はここに入れない。** 色モードは 1 枚 2.6 秒なので 58 枚で
                # 150 秒かかり、一覧が開かなくなる。scene を開いたときに別途取る。
                pq = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("pool")
                self._json(200, worklist(int(pq[0]) if pq and pq[0].isdigit() else None))
            elif p.startswith("/api/derived/"):
                self._json(200, derived_facts(int(p.rsplit("/", 1)[1])))
            elif p == "/api/build":
                # 画面が古い版かどうかを**推測せずに**言えるようにする
                self._json(200, {"build": BUILD})
            elif p.startswith("/api/scene_obs/"):
                self._json(200, scene_observations(int(p.rsplit("/", 1)[-1])))
            elif p.startswith("/api/pattern/"):
                # 0072: 型に当たる post を返す — 分解された所見を 1 語で引く
                self._json(200, pattern_hits(p.rsplit("/", 1)[-1]))
            elif p.startswith("/api/propose/"):
                self._json(200, propose_subjects(int(p.rsplit("/", 1)[1])))
            elif p == "/api/vocab":
                v = vocab()
                body = json.dumps(v, ensure_ascii=False).encode()
                # 内容の指紋で 304 を返せるようにする (53 KB を毎回送らない)
                tag = '"v%s"' % __import__("hashlib").sha1(body).hexdigest()[:16]
                self._send(200, body, "application/json",
                           cache="private, must-revalidate", etag=tag)
            elif p.startswith("/pool/"):
                self._send(200, POOL_PAGE.replace("__POOL_ID__", str(int(p.rsplit("/", 1)[1])))
                           .encode(), "text/html; charset=utf-8")
            elif p.startswith("/api/pool_anchor/"):
                self._json(200, pool_anchor_data(int(p.rsplit("/", 1)[1])))
            elif p.startswith("/api/pool_obs/"):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                self._json(200, pool_observations(
                    int(p.rsplit("/", 1)[1]),
                    (q.get("attr") or ["flush_intensity"])[0],
                    (q.get("region") or ["face"])[0]))
            elif p.startswith("/api/ann/"):
                pid = int(p.rsplit("/", 1)[1])
                f = ANN_DIR / f"{pid}.json"
                self._json(200, json.loads(f.read_text(encoding="utf-8")) if f.exists() else {})
            elif p.startswith("/img/"):
                pid = int(p.rsplit("/", 1)[1])
                hits = image_path(pid)
                if not hits:
                    self._send(404, b"no image", "text/plain")
                    return
                st = hits[0].stat()
                # ファイル名に hash が入っており内容は変わらない。変わったとしても
                # mtime/size が変わるので ETag が変わる。
                want = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("w")
                w = int(want[0]) if want and want[0].isdigit() else 0
                if w:
                    body, ctype = thumb(hits[0], w)
                    tag = f'"{hits[0].name}-{int(st.st_mtime)}-w{w}"'
                else:
                    ext = hits[0].suffix.lower()
                    ctype = {".png": "image/png", ".webp": "image/webp",
                             ".gif": "image/gif"}.get(ext, "image/jpeg")
                    body = hits[0].read_bytes()
                    tag = f'"{hits[0].name}-{int(st.st_mtime)}-{st.st_size}"'
                self._send(200, body, ctype,
                           cache="private, max-age=604800", etag=tag)
            else:
                self._send(404, b"not found", "text/plain")
        except Exception:
            self._send(500, traceback.format_exc().encode(), "text/plain; charset=utf-8")

    def _retract(self) -> None:
        """所見をワンタップで撤回する (人間の判断)。**人間名義で撤回観測を足す** —
        モデルの誤りを人間が消す経路 (append-only なので消さず、撤回で上書き)。
        理由は必須 (retract.py と同じ規律。理由の無い撤回は後から検算できない)。
        """
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n))
            oid = int(body["observation"])
            why = (body.get("why") or "").strip()
        except Exception:
            self._json(400, {"ok": False, "error": "observation と why が要る"})
            return
        if not why:
            self._json(400, {"ok": False, "error": "why (なぜ誤りか) は必須"})
            return
        try:
            with psycopg.connect(DSN, autocommit=False) as c, c.cursor() as cur:
                cur.execute("SET search_path = kura, public")
                cur.execute("SELECT scene_id, subject_scope, subject_ref, domain, event_type"
                            "  FROM observation WHERE id=%s", (oid,))
                row = cur.fetchone()
                if not row:
                    self._json(404, {"ok": False, "error": f"observation {oid} が無い"})
                    return
                sid, scope, ref, dom, ev = row
                if ev == "retraction":
                    self._json(400, {"ok": False, "error": "撤回観測は撤回しない"})
                    return
                cur.execute("SELECT count(*) FROM observation"
                            " WHERE event_type='retraction' AND retracts_id=%s", (oid,))
                if cur.fetchone()[0]:
                    self._json(200, {"ok": True, "already": True})
                    return
                cur.execute(
                    "INSERT INTO observation(scene_id, subject_scope, subject_ref, domain,"
                    " event_type, producer_kind, producer_ref, retracts_id)"
                    " VALUES (%s,%s,%s,%s,'retraction','human',%s,%s) RETURNING id",
                    (sid, scope, ref, dom, f"retract: {why}", oid))
                new_id = cur.fetchone()[0]
                c.commit()
            self.note(f"retract obs={oid} → {new_id}: {why[:60]}")
            self._json(200, {"ok": True, "retraction_id": new_id})
        except Exception:
            self._json(500, {"ok": False, "error": traceback.format_exc()[-300:]})

    # 各 domain の子テーブルと、コピーする列。observation_id は差し替える。
    _ADOPT_CHILD = {
        "identity": ("obs_identity",
            "species_slug,character_slug,modifier_slug,entity_kind,presentation,motif_species_slug"),
        "body_region": ("obs_body_region", "region_slug,present"),
        "pose": ("obs_pose", "pose_slug,laterality"),
        "attribute": ("obs_attribute",
            "attribute_slug,value_slug,value_numeric,interval_low,interval_high,"
            "region_slug,laterality,text_content"),
        "relation": ("obs_relation",
            "relation_slug,subject_ref,subject_region_slug,object_ref,object_region_slug,"
            "subject_laterality,object_laterality,object_surface_slug"),
        "scene_descriptor": ("obs_scene_descriptor", "descriptor_slug"),
        "entity_count": ("obs_entity_count", "presentation,count_min,count_exact,involvement"),
    }

    def _adopt(self) -> None:
        """モデル観測を**人間名義でも記録する** (採用 = 明示的な確認)。
        無言の一致に頼らず、モデル観測ごとに人間の判定を付ける (P4 の教師信号)。
        append-only — モデル観測はそのまま、人間の観測を新たに足す。冪等。
        """
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n))
            mid = int(body["observation"])
            who = (body.get("annotator") or "desk").strip()
        except Exception:
            self._json(400, {"ok": False, "error": "observation が要る"})
            return
        try:
            with psycopg.connect(DSN, autocommit=False) as c, c.cursor() as cur:
                cur.execute("SET search_path = kura, public")
                cur.execute("SELECT scene_id, subject_scope, subject_ref, subject_asset_region_id,"
                            " domain, producer_kind FROM observation WHERE id=%s", (mid,))
                row = cur.fetchone()
                if not row:
                    self._json(404, {"ok": False, "error": f"observation {mid} が無い"})
                    return
                sid, scope, ref, arid, dom, pk = row
                if pk != "model":
                    self._json(400, {"ok": False, "error": "採用はモデル観測にだけ"})
                    return
                child = self._ADOPT_CHILD.get(dom)
                if not child:
                    self._json(400, {"ok": False, "error": f"未対応の domain {dom}"})
                    return
                tbl, cols = child
                # **冪等**: 既に human が同じ内容 (子テーブルの全列が一致する live 観測) を
                # 持っていれば作らない。子テーブルの列で mid と突き合わせる。
                colnames = [c.strip() for c in cols.split(",")]
                eq = " AND ".join(
                    f"ch.{c} IS NOT DISTINCT FROM m.{c}" for c in colnames)
                cur.execute(
                    f"SELECT 1 FROM observation o"
                    f" JOIN {tbl} ch ON ch.observation_id=o.id"
                    f" JOIN {tbl} m ON m.observation_id=%s"
                    f" WHERE o.scene_id=%s AND o.producer_kind='human' AND o.domain=%s"
                    f"   AND coalesce(o.subject_ref,'')=coalesce(%s,'')"
                    f"   AND o.retracts_id IS NULL"
                    f"   AND NOT EXISTS(SELECT 1 FROM observation r WHERE r.retracts_id=o.id)"
                    f"   AND {eq} LIMIT 1",
                    (mid, sid, dom, ref))
                if cur.fetchone():
                    self._json(200, {"ok": True, "already": True})
                    return
                # 新しい human 観測
                cur.execute(
                    "INSERT INTO observation(scene_id, subject_scope, subject_ref,"
                    " subject_asset_region_id, domain, event_type, producer_kind,"
                    " producer_ref, adopted_from_id)"   # 元の model 観測への参照 (0086)
                    " VALUES (%s,%s,%s,%s,%s,'human_annotation','human',%s,%s)"
                    " RETURNING id",
                    (sid, scope, ref, arid, dom, who, mid))
                nid = cur.fetchone()[0]
                cur.execute(
                    f"INSERT INTO {tbl}(observation_id,{cols})"
                    f" SELECT %s,{cols} FROM {tbl} WHERE observation_id=%s", (nid, mid))
                c.commit()
            self.note(f"adopt model obs={mid} -> human {nid} ({dom})")
            self._json(200, {"ok": True, "human_id": nid})
        except Exception:
            self._json(500, {"ok": False, "error": traceback.format_exc()[-300:]})

    def _quick_attr(self) -> None:
        """pool オブザベーションビューからの人物×属性のワンクリック付与。

        人間のクリック判断を human 名義で 1 観測書く (通常 UI の確定と同じ出所)。
        dry=true は validate まで (smoke 用、書かない)。
        """
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            b = json.loads(self.rfile.read(ln) or b"{}")
            pid, ref = int(b["post"]), str(b["ref"])
            attr, val = str(b["attribute"]), str(b["value"])
            region = str(b.get("region") or "") or None
            entry: dict = {"attribute": attr, "value": val}
            if region:
                entry["region"] = region
            ann = {"kura_post_id": pid, "annotator": "desk",
                   "producer_kind": "human", "producer_ref": "desk",
                   "minutes_unmeasured": "pool-view",
                   "subjects": [{"ref": ref, "kind": "person", "regions": [],
                                 "attributes": [entry], "poses": [], "objects": []}],
                   "relations": [], "scene_descriptors": [], "entity_counts": [],
                   "unresolved": []}
            with psycopg.connect(DSN, autocommit=False) as c, c.cursor() as cur:
                cur.execute("SET search_path = kura, public")
                errs = A.validate(ann, A.Ontology(cur))
                if errs:
                    self._json(400, {"ok": False, "error": errs[0]})
                    return
                if b.get("dry"):
                    self._json(200, {"ok": True, "dry": True})
                    return
                got = A.write(cur, ann)
                c.commit()
            self._json(200, {"ok": True, "written": got["observation"],
                             "skipped": got["skipped"]})
        except Exception as e:
            traceback.print_exc()
            self._json(500, {"ok": False, "error": str(e)[:200]})

    def _quick_anchor(self) -> None:
        """位置版ビューからの頭中心 anchor 付与 (human 名義)。

        A.write の dedup は内容キーだけを見るため、既に head 部位所見がある人物へ
        anchor だけ足すと asset_region が孤児になる (観測から参照されない)。だから
        ここは **anchor つきの body_region=head 観測を明示的に 1 行書く** —
        「頭がそこに見えている」は user のクリックが証明する真の観測で、
        採点の region 軸は set 意味論なので重複 head 所見は数を変えない。
        """
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            b = json.loads(self.rfile.read(ln) or b"{}")
            pid, ref = int(b["post"]), str(b["ref"])
            x, y = float(b["x"]), float(b["y"])
            # user の付与方針 (2026-08-01): 頭が見えれば頭、無ければ腰、それも無ければ
            # 体の一部。region を固定 'head' にすると「頭が見えている」という偽観測を
            # 書いてしまうので、指した部位を受け取る ('body' = 部位不明の体)
            region = str(b.get("region") or "head")
            if region not in ("head", "waist", "body"):
                self._json(400, {"ok": False, "error": "region は head/waist/body"})
                return
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                self._json(400, {"ok": False, "error": "x/y は 0..1"})
                return
            with psycopg.connect(DSN, autocommit=False) as c, c.cursor() as cur:
                cur.execute("SET search_path = kura, public")
                cur.execute(
                    "SELECT a.id, a.scene_id FROM asset a"
                    " JOIN asset_source s ON s.asset_id=a.id"
                    " WHERE s.source_slug='kura_post' AND s.external_id=%s", (str(pid),))
                row = cur.fetchone()
                if not row:
                    self._json(404, {"ok": False, "error": f"post {pid} 未import"})
                    return
                asset_id, scene_id = row
                # ref は human の既知人物に限る (勝手な ref を作らせない)
                cur.execute(
                    "SELECT 1 FROM observation WHERE scene_id=%s AND subject_ref=%s"
                    "   AND producer_kind='human' AND retracts_id IS NULL LIMIT 1",
                    (scene_id, ref))
                if not cur.fetchone():
                    self._json(400, {"ok": False, "error": f"{ref} は human の人物に無い"})
                    return
                if b.get("dry"):
                    self._json(200, {"ok": True, "dry": True})
                    return
                # 付け直し: この ref に quick-anchor 由来の旧観測があれば撤回する
                # (偽 region ラベルの自己修復経路。旧 anchor は履歴として残る)
                cur.execute(
                    "SELECT o.id FROM observation o"
                    " JOIN asset_region ar ON ar.id = o.subject_asset_region_id"
                    " WHERE o.scene_id=%s AND o.subject_ref=%s"
                    "   AND o.producer_kind='human' AND o.producer_ref='desk'"
                    "   AND o.domain IN ('body_region','identity')"
                    "   AND ar.anchor_x IS NOT NULL AND o.retracts_id IS NULL"
                    "   AND NOT EXISTS (SELECT 1 FROM observation r"
                    "                    WHERE r.retracts_id=o.id)",
                    (scene_id, ref))
                for (old_oid,) in cur.fetchall():
                    cur.execute(
                        "INSERT INTO observation(scene_id, subject_scope, subject_ref,"
                        " domain, event_type, producer_kind, producer_ref, retracts_id)"
                        " VALUES (%s,'entity',%s,'body_region','retraction','human',"
                        " 'retract: anchor 付け直し (pool view)',%s)",
                        (scene_id, ref, old_oid))
                cur.execute(
                    "INSERT INTO asset_region(asset_id, anchor_x, anchor_y,"
                    " anchor_region_slug) VALUES (%s,%s,%s,%s) RETURNING id",
                    (asset_id, round(x, 4), round(y, 4),
                     None if region == "body" else region))
                arid = cur.fetchone()[0]
                if region in ("head", "waist"):
                    # 指せた = その部位が見えている (真の観測)
                    cur.execute(
                        "INSERT INTO observation(scene_id, subject_scope, subject_ref,"
                        " subject_asset_region_id, domain, event_type, producer_kind,"
                        " producer_ref) VALUES (%s,'entity',%s,%s,'body_region',"
                        " 'human_annotation','human','desk') RETURNING id",
                        (scene_id, ref, arid))
                    oid = cur.fetchone()[0]
                    cur.execute(
                        "INSERT INTO obs_body_region(observation_id, region_slug,"
                        " present) VALUES (%s,%s,TRUE)", (oid, region))
                else:
                    # 部位不明 (body): 部位の主張はせず、既存 identity (species) の
                    # 再観測を position の運搬体にする — 内容は真の複製で、
                    # 採点は set 意味論なので数に影響しない
                    cur.execute(
                        "SELECT oi.species_slug FROM observation o"
                        " JOIN obs_identity oi ON oi.observation_id=o.id"
                        " WHERE o.scene_id=%s AND o.subject_ref=%s"
                        "   AND o.producer_kind='human' AND oi.species_slug IS NOT NULL"
                        "   AND o.retracts_id IS NULL"
                        " ORDER BY o.id DESC LIMIT 1", (scene_id, ref))
                    sp = cur.fetchone()
                    if not sp:
                        self._json(400, {"ok": False,
                                         "error": f"{ref} に identity が無く body anchor を運べない"})
                        return
                    cur.execute(
                        "INSERT INTO observation(scene_id, subject_scope, subject_ref,"
                        " subject_asset_region_id, domain, event_type, producer_kind,"
                        " producer_ref) VALUES (%s,'entity',%s,%s,'identity',"
                        " 'human_annotation','human','desk') RETURNING id",
                        (scene_id, ref, arid))
                    oid = cur.fetchone()[0]
                    cur.execute(
                        "INSERT INTO obs_identity(observation_id, species_slug)"
                        " VALUES (%s,%s)", (oid, sp[0]))
                c.commit()
            self._json(200, {"ok": True, "observation": oid, "region": region})
        except Exception as e:
            traceback.print_exc()
            self._json(500, {"ok": False, "error": str(e)[:200]})

    def do_POST(self) -> None:  # noqa: N802
        p = urllib.parse.urlparse(self.path).path
        if p == "/api/retract":
            self._retract()
            return
        if p == "/api/adopt":
            self._adopt()
            return
        if p == "/api/quick_attr":
            self._quick_attr()
            return
        if p == "/api/quick_anchor":
            self._quick_anchor()
            return
        if p == "/api/draft":          # navigator.sendBeacon は POST で来る
            self.note("draft(POST)")
            self.do_PUT()
            return
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n)
        try:
            ann = json.loads(raw)
        except Exception as e:
            self._json(400, {"errors": [f"JSON として読めません: {e}"]})
            return
        # ── 補完ページからの確定 ────────────────────────────────
        # 下書きに人数と目の色を**足してから**、通常の /api/commit と同じ経路を通す。
        # 別経路で直接 INSERT すると検証を迂回してしまうので、必ず write() を通す。
        if p == "/api/fill_commit":
            try:
                pid = int(ann["post_id"])
                f = ANN_DIR / f"{pid}.json"
                d = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
                d["kura_post_id"] = pid
                d.setdefault("annotator", "desk")
                if d.get("minutes") is None:
                    d["minutes_unmeasured"] = (d.get("minutes_unmeasured")
                        or "補完ページからの追記 — この分の所要時間は計測していない")
                # 人数は書かない — **entity を数えれば出る** (0037)。
                # entity_count は「列挙できない群衆」専用。
                for e in ann.get("presentations", []):
                    for s in d.get("subjects", []):
                        if s.get("ref") == e["ref"]:
                            s["presentation"] = e["value"]
                f.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
                ann = d
            except Exception:
                self._json(500, {"errors": ["補完の反映で例外: "
                                            + traceback.format_exc()[-500:]]})
                return
            p = "/api/commit"
        try:
            pid = int(ann.get("kura_post_id", 0))
            with psycopg.connect(DSN, autocommit=False) as conn, conn.cursor() as cur:
                cur.execute("SET search_path = kura, public")
                errs = A.validate(ann, A.Ontology(cur))
                if p == "/api/validate":
                    self._json(200, {"errors": errs})
                    return
                if p != "/api/commit":
                    self._send(404, b"not found", "text/plain")
                    return
                if errs:
                    self.note(f"commit post={pid} 検証エラー {len(errs)} 件: {errs[0][:80]}")
                    self._json(200, {"errors": errs})
                    return
                # 下書きは常に保存しておく (投入に失敗しても手作業が消えないように)
                (ANN_DIR / f"{pid}.json").write_text(
                    json.dumps(ann, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                got = A.write(cur, ann)
                conn.commit()
            self.note(f"commit post={pid} 成功 {got}")
            self._json(200, {"errors": [], "written": got})
        except Exception:
            # **例外も記録する。** ここを記録していなかったので「確定できません」が
            # 出ているのにログが空、という切り分け不能な状態になった (2026-07-31)
            tb = traceback.format_exc()
            self.note(f"commit 例外: {tb.strip().splitlines()[-1][:160]}")
            self._json(500, {"errors": ["投入中に例外: " + tb[-800:]]})

    def do_PUT(self) -> None:  # noqa: N802
        """下書き保存 (投入しない)。"""
        n = int(self.headers.get("Content-Length", 0))
        try:
            ann = json.loads(self.rfile.read(n))
            pid = int(ann["kura_post_id"])
            (ANN_DIR / f"{pid}.json").write_text(
                json.dumps(ann, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self._json(200, {"ok": True})
        except Exception as e:
            self._json(400, {"ok": False, "error": str(e)})


PAGE = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>kura1 注釈</title>
<style>
:root{--hh:104px;--bg:#14161a;--panel:#1b1e24;--line:#2b3038;--fg:#dfe3ea;--dim:#8b94a3;
      --acc:#5fd68a;--warn:#f0a050;--err:#f07a7a;--sel:#7aa8f0}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}
/* **2 行にする。** 1 行だと他のコントロールと幅を取り合ってサムネイルが 3 枚しか
   入らなかった (2026-07-31 実測)。1 行目 = 操作、2 行目 = サムネイル帯 (全幅)。 */
header{display:flex;gap:12px;align-items:center;padding:6px 14px 0;background:var(--panel);
       border-bottom:1px solid var(--line);position:fixed;top:0;left:0;right:0;height:var(--hh);
       z-index:40;flex-wrap:wrap;overflow:hidden;row-gap:4px}
header b{font-size:15px}
.pill{border:1px solid var(--line);border-radius:999px;padding:2px 10px;color:var(--dim);cursor:pointer}
.pill{padding:1px 8px;font-size:12px}
/* いま開いている post 番号。番号だけの pill では照合できないので見出しに出す */
.postno{font-weight:700;font-size:14px;letter-spacing:.02em;text-decoration:none;
        border:1px solid var(--line);border-radius:4px;padding:1px 8px;color:var(--fg)}
.postno:hover{border-color:var(--sel);color:var(--sel)}
.postno.st-done{border-color:var(--acc);color:var(--acc)}
.postno.st-draft{border-color:var(--warn);color:var(--warn)}
.pill.st-todo{border-color:var(--line);color:var(--dim)}
/* 層D は utility の分子に入らないので後回し。薄くして視線を奪わない
   (落とすのではなく順序 — タガーの評価 GT としては一番価値がある) */
.pill.later{opacity:.42;border-style:dashed}
.pill.st-draft{border-color:var(--warn);color:var(--warn)}
.pill.st-done{border-color:var(--acc);color:var(--acc);background:rgba(95,214,138,.12)}
.pill.cur{outline:2px solid var(--sel);outline-offset:1px}
/* 58 個の pill で折り返すとヘッダが --hh (44px) を超え、**パネルの裏に潜る**
   (2026-07-28 実測)。1 行に固定して横スクロールさせ、高さを一定に保つ。 */
/* `.row` が後ろで flex-wrap:wrap を掛けるので id で特異度を上げる */
/* サムネイル帯は**自分の行を丸ごと使う** (flex-basis:100%)。order で最後に回す */
#nav.navwrap{flex:0 0 100%;order:9;min-width:0;flex-wrap:nowrap;gap:3px;overflow-x:auto;
         overflow-y:hidden;margin:0 -14px;padding:0 14px 4px}
#nav.navwrap .pill{flex:0 0 auto}
/* 画像は **ビューポート基準**で大きさが決まる。コントロールは position:fixed の
   オーバーレイなので、選択肢の文字列が伸び縮みしても画像サイズは一切動かない
   (旧: grid の 1fr 列が中身に押されて画像列が痩せていた)。 */
main{position:fixed;inset:var(--hh) 0 0 0;display:flex;align-items:center;justify-content:center;
     padding:8px;overflow:hidden}
/* touch-action:none が無いと、Chrome が「パン開始」と判断して pointercancel を投げ、
   ドラッグが 1 歩で切れる (bbox が極小になる) */
#stage{position:relative;display:inline-block;max-width:100%;max-height:100%;line-height:0;
       touch-action:none}
/* -webkit-user-drag:none が無いと、bbox を描こうとした瞬間にブラウザ標準の
   「画像のドラッグ&ドロップ」が始まり、半透明のゴースト画像を掴んでしまう
   (2026-07-28 指摘)。user-select:none だけでは止まらない。 */
#stage img{display:block;max-width:100%;max-height:calc(100vh - var(--hh) - 16px);
           width:auto;height:auto;user-select:none;-webkit-user-drag:none;user-drag:none;
           background:#0d0f12;border:1px solid var(--line);border-radius:6px}
/* boxes は img と同じ矩形に重なる (#stage が画像に shrink-wrap している) */
#boxes{position:absolute;inset:0;pointer-events:none}
/* **色は人物の identity。** 2 人目以降が全部同じ色だと画像上で誰の印か判らない
   (2026-07-29 指摘)。色は --c で人物ごとに渡し、**選択中は色を変えずに太さと影**で
   示す — 選択で色が変わると「色 = 人物」が崩れて識別に使えなくなる。 */
.bx{position:absolute;border:2px solid var(--c);background:color-mix(in srgb,var(--c) 12%,transparent)}
.bx.act{border-width:3px;box-shadow:0 0 0 2px rgba(0,0,0,.55),0 0 12px var(--c)}
.bx span{position:absolute;top:-1px;left:-1px;background:var(--c);color:#07090c;
         font-size:11px;padding:0 5px;font-weight:700;white-space:nowrap;
         text-shadow:none;border-radius:0 0 3px 0}
/* 位置の「点」— 十字と輪。box と違い重なった人物でも別々の位置に立つ */
.pt{position:absolute;width:0;height:0}
.pt::before,.pt::after{content:'';position:absolute;background:var(--c);
  box-shadow:0 0 0 1px rgba(0,0,0,.85)}
.pt::before{left:-13px;top:-1px;width:26px;height:2px}
.pt::after{left:-1px;top:-13px;width:2px;height:26px}
.pt.act::before{left:-17px;width:34px;height:3px;top:-1px}
.pt.act::after{top:-17px;height:34px;width:3px;left:-1px}
.pt.act{filter:drop-shadow(0 0 6px var(--c))}
/* 点そのものの位置を示す輪 (十字の交点が隠れないように中抜き) */
.pt i{position:absolute;left:-5px;top:-5px;width:10px;height:10px;border-radius:50%;
  border:2px solid var(--c);box-shadow:0 0 0 1px rgba(0,0,0,.85);font-style:normal}
.pt span{position:absolute;left:11px;top:-22px;background:var(--c);color:#07090c;
  font-size:11px;padding:0 5px;font-weight:700;white-space:nowrap;text-shadow:none;
  border-radius:3px}
section{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:10px 12px;margin-bottom:12px}
h3{margin:0 0 8px;font-size:13px;color:var(--dim);letter-spacing:.04em;text-transform:uppercase}
label{display:block;font-size:12px;color:var(--dim);margin:6px 0 2px}
input,select,textarea,button{font:inherit;color:var(--fg);background:#0f1216;border:1px solid var(--line);
  border-radius:4px;padding:4px 6px}
input:focus,select:focus,textarea:focus{outline:1px solid var(--sel)}
textarea{width:100%;min-height:52px;resize:vertical}
button{cursor:pointer}
button.go{background:#16301f;border-color:var(--acc);color:var(--acc);padding:6px 14px;font-weight:600}
button.go.blocked{background:#2a1618;border-color:var(--err);color:var(--err)}
button.sub{background:#1a2230;border-color:var(--sel);color:var(--sel)}
/* header の「原寸」は a 要素。button.sub と同じ見え方にする */
header a.sub{background:#1a2230;border:1px solid var(--sel);color:var(--sel);
  border-radius:4px;padding:3px 9px;text-decoration:none;font-size:12px}
button.del{background:#2a1618;border-color:var(--err);color:var(--err);padding:1px 7px}
.row{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:4px 0}
.tag{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--line);border-radius:4px;
     padding:1px 4px 1px 7px;background:#0f1216;font-size:12px}
.tag i{cursor:pointer;color:var(--dim);font-style:normal}
.tag i:hover{color:var(--err)}
.subj{border:1px solid var(--line);border-left:5px solid var(--c);border-radius:5px;
       padding:8px;margin-bottom:8px}
.subj.act{border-color:var(--acc);border-left-color:var(--c)}
/* カード側にも色の見本を出す — 画像上の印と突き合わせるのに色名は要らない */
.who{display:inline-flex;align-items:center;gap:5px;font-weight:700;color:var(--c);
     text-shadow:0 1px 2px rgba(0,0,0,.95)}
.who b{width:11px;height:11px;border-radius:50%;background:var(--c);
       box-shadow:0 0 0 1px rgba(0,0,0,.8)}
.subj header{background:none;border:0;padding:0;position:static;justify-content:space-between}
#msg{white-space:pre-wrap;font-size:12.5px}
#msg .e{color:var(--err)}
#msg .k{color:var(--acc)}
.hint{color:var(--dim);font-size:12px}
/* ── コントロールのオーバーレイ ────────────────────────────────
   既定は半透明で画像が透ける。**マウスオーバーした section はさらに透明**になり、
   指している場所の画像が見える (文字は text-shadow で読ませる — バーで塗り潰して
   可読性を稼ぐと画像の確認を邪魔するため。lightbox と同じ方針)。 */
/* コントロールが増えすぎて片側では縦に長すぎるので **左右に分ける** (2026-07-28 指摘)。
   右 = 人物 (部位・属性・ポーズ。最も長い)、左 = 関係 / 画面全体 / unresolved / メモ / 結果。
   画像は中央に残るので、真ん中の帯は常に素通しで見える。 */
#panel,#lpanel{position:fixed;top:var(--hh);bottom:0;
       overflow-y:auto;overscroll-behavior:contain;padding:10px 12px 40px;z-index:20;
       transition:opacity .15s}
/* 人物 (長い) が右、それ以外 (短い) が左なので幅も非対称にする */
#panel{width:min(680px,44vw)}
#lpanel{width:min(460px,27vw)}
#panel{right:0;border-left:1px solid var(--line);
       background:linear-gradient(to left,rgba(12,14,18,.56),rgba(12,14,18,.32))}
#lpanel{left:0;border-right:1px solid var(--line);
       background:linear-gradient(to right,rgba(12,14,18,.56),rgba(12,14,18,.32))}
#panel.hide,#lpanel.hide{opacity:0;pointer-events:none}
#panel section,#lpanel section{background:rgba(20,23,28,.38);backdrop-filter:none;
  transition:background .15s}
/* 指している所は画像を透かす */
#panel section:hover,#lpanel section:hover{background:rgba(20,23,28,.10)}
/* 地を薄くしたぶん影を強くして可読性を保つ (バーで塗り潰すと画像の確認を邪魔する) */
#panel,#panel *,#lpanel,#lpanel *{text-shadow:0 1px 2px rgba(0,0,0,.95),0 0 6px rgba(0,0,0,.85)}
#panel .cb-btn,#panel input,#panel textarea,
#lpanel .cb-btn,#lpanel input,#lpanel textarea{background:rgba(10,13,16,.58)}
#panel section:hover .cb-btn,#panel section:hover input,#panel section:hover textarea,
#lpanel section:hover .cb-btn,#lpanel section:hover input,#lpanel section:hover textarea{
  background:rgba(10,13,16,.28)}
#peek{position:fixed;right:8px;top:calc(var(--hh) + 6px);z-index:30}
/* ── 自前 combobox。ネイティブ select のポップアップは CSS が効かないので置換した ── */
.cb{display:inline-flex;min-width:0;max-width:100%}
.cb-btn{width:100%;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
        border:1px solid var(--line);border-radius:4px;padding:4px 6px;cursor:pointer}
.cb-btn.empty{color:var(--dim)}
.morow{display:flex;flex-wrap:wrap;gap:5px;align-items:center;margin:3px 0 6px;
  padding:3px 6px;border-left:2px solid #d0812f;background:rgba(208,129,47,.06)}
.moh{font-style:normal;font-size:10px;font-weight:700;color:#d0812f;flex:0 0 auto}
.moh2{font-size:10px;color:var(--dim);flex:0 0 auto;margin-left:4px}
.moc{font-size:11px;padding:1px 6px;border-radius:4px}
.moc.ok{color:#8fe0aa;border:1px solid rgba(95,214,138,.4)}
.moc.bad{color:#ffcaa0;border:1px solid #d0812f;font-weight:600}
.mchip{font-size:11px;padding:1px 6px;border-radius:4px;cursor:default}
.mchip.ok{color:#8fe0aa;border:1px solid rgba(95,214,138,.35)}
.mchip.bad{color:#ffcaa0;border:1px solid #d0812f;cursor:pointer}
.mchip.bad:hover{background:rgba(208,129,47,.2)}
/* 突合チップ: 人=青 (arm-H と同じ系統)。モは既存の ok/bad をそのまま使う */
.mchip.hu{color:#a9c9ee;border:1px solid rgba(96,146,214,.5)}
.mchip .mic{cursor:pointer;font-style:normal;font-weight:700;margin-left:4px}
.mchip .mic.ok{color:#8fe0aa} .mchip .mic.no{color:#ff9d9d}
.mchip .mic:hover{text-decoration:underline}
/* 一致印: 下書きに既にある値は突合行へ再掲せず、その行・タグに極小の印だけ付ける */
.hm-m{font-style:normal;font-size:9px;font-weight:700;margin-left:3px;
  padding:0 3px;border-radius:3px;vertical-align:1px;cursor:help}
.hm-m.hu{color:#a9c9ee;border:1px solid rgba(96,146,214,.5)}
.hm-m.mo{color:#8fe0aa;border:1px solid rgba(95,214,138,.4)}
/* ✓✗ の取り消し猶予トースト */
.undo-toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);
  background:#2a2f3a;border:1px solid #d0812f;color:#ffcaa0;padding:8px 12px;
  border-radius:8px;z-index:99;display:flex;gap:10px;align-items:center;font-size:13px}
.arm{position:absolute;top:2px;left:2px;font-style:normal;font-weight:700;font-size:10px;
  padding:0 4px;border-radius:3px;color:#fff}
.arm-H{background:#2f6fd0} .arm-M{background:#d0812f}
.th{position:relative}
.pdrow{position:absolute;top:2px;right:2px;display:flex;gap:2px}
.pd{width:8px;height:8px;border-radius:50%;box-shadow:0 0 0 1px #000a;display:inline-block}
.pd-model{background:#d0812f} .pd-human{background:#5fd68a}
.armf{margin:0 2px;padding:1px 6px;border-radius:4px;border:1px solid var(--line);color:var(--dim)}
.armf.on{border-color:var(--sel);color:var(--sel)}
.obs-line{display:flex;gap:8px;align-items:baseline;padding:2px 6px;border-bottom:1px solid var(--line)}
.obs-tag{font-style:normal;font-size:10px;font-weight:700;padding:0 5px;border-radius:3px;color:#fff;flex:0 0 auto}
.obs-model .obs-tag,.pd-model-t{background:#d0812f;color:#fff;padding:0 4px;border-radius:3px}
.obs-human .obs-tag,.pd-human-t{background:#3a8f56;color:#fff;padding:0 4px;border-radius:3px}
.obs-model{background:rgba(208,129,47,.08)}
.obs-x{margin-left:auto;font-size:11px;padding:1px 6px;border:1px solid var(--err);
  color:#ff9d9d;background:transparent;border-radius:4px;cursor:pointer;flex:0 0 auto}
.obs-ic{margin-left:2px;width:20px;height:20px;line-height:1;padding:0;border-radius:4px;
  cursor:pointer;flex:0 0 auto;font-weight:700;background:transparent}
.obs-line .obs-ic.ok{margin-left:auto;color:#8fe0aa;border:1px solid rgba(95,214,138,.5)}
.obs-ic.ok:hover{background:rgba(95,214,138,.2)}
.obs-ic.no{color:#ff9d9d;border:1px solid var(--err)}
.obs-ic.no:hover{background:rgba(240,122,122,.2)}
.rjmenu{position:fixed;z-index:300;display:flex;flex-direction:column;gap:2px;padding:4px;
  background:rgba(14,17,22,.96);border:1px solid var(--line);border-radius:6px;
  box-shadow:0 8px 28px rgba(0,0,0,.6)}
.rjb{font-size:12px;text-align:left;padding:3px 10px;border:1px solid transparent;
  border-radius:4px;cursor:pointer;background:transparent;color:var(--fg,#dfe3ea)}
.rjb:hover{border-color:var(--sel)}
.rjb.w-strong{color:#ffb0b0} .rjb.w-weak{color:var(--dim)}
.stale{background:#4a1414;border:1px solid var(--err);color:#ffd0d0;
  border-radius:4px;padding:3px 9px;font-weight:600;cursor:pointer}
#findout .f-line{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;
  padding:3px 6px;border-bottom:1px solid var(--line)}
#findout .f-w{color:var(--sel);font-size:11px;flex:0 0 auto}
#findout .f-d{font-style:normal;color:var(--acc);font-size:11.5px;opacity:.85;
  flex:1 1 200px;min-width:0}
#findout{max-height:46vh;overflow:auto}
#sayout .bad{color:#ff9d9d;margin-top:4px}
#sayout .k{color:var(--sel)}
.cb-i i code,.desc code{font-family:ui-monospace,monospace;font-size:.92em;
  background:rgba(255,255,255,.10);border-radius:3px;padding:0 3px}
.cb-i i b,.desc b{color:var(--fg,#e8edf2);font-weight:700}
/* header の combobox は**幅を固定**する。ここが伸びると header の flex が折り返して
   カード全体が崩れる (2026-07-30 の creature 崩れの再発防止。文字列側の切り詰めと
   二重の守り — どちらか片方だけだと語彙が長くなったときに再発する) */
header .cb{max-width:210px;flex:0 1 210px}
.cb-btn:hover{border-color:var(--sel)}
/* **半透明** — 選んでいる間も下の画像が見える。ぼかしは入れない (画像を鮮明に保つ) */
.cb-pop{position:fixed;z-index:200;display:flex;flex-direction:column;overflow:hidden;
        background:rgba(14,17,22,.58);border:1px solid rgba(255,255,255,.22);border-radius:6px;
        box-shadow:0 8px 28px rgba(0,0,0,.55)}
.cb-pop:hover{background:rgba(14,17,22,.40)}
.cb-search{margin:6px;flex:0 0 auto;background:rgba(8,10,13,.70)}
.cb-list{overflow-y:auto;padding:0 4px 6px}
.cb-g{position:sticky;top:0;background:rgba(14,17,22,.80);color:var(--sel);
      font-size:11px;letter-spacing:.05em;padding:4px 6px 2px;border-bottom:1px solid var(--line);
      /* 見出しは「属性 — 説明」なので長い。1 行に抑えて全文は title で出す */
      white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cb-i{padding:3px 7px;border-radius:4px;cursor:pointer;display:flex;gap:8px;align-items:baseline}
.cb-i b{font-weight:600;flex:0 0 auto}
/* 人物の色見本。選択肢の中でも、選んだ後のボタンでも同じものを出す */
.dot{flex:0 0 auto;width:9px;height:9px;border-radius:50%;background:var(--c);
     box-shadow:0 0 0 1px rgba(0,0,0,.8);display:inline-block;margin-right:5px;
     vertical-align:baseline}
.cb-btn .dot{margin-right:6px}
.cb-i{align-items:flex-start}
.cb-i i{font-style:normal;color:var(--acc);font-size:11.5px;opacity:.85;
        overflow:hidden;min-width:0;
        /* **1 行に潰さない。** nowrap+ellipsis だった版は語彙の説明がほぼ全部
           「…」で切れて読めなかった (user 指摘 2026-07-31)。2 行までは出す */
        display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2}
.cb-i:hover,.cb-i.cur{background:rgba(122,168,240,.22)}
.cb-i.on{outline:1px solid var(--acc)}
.cb-none{padding:8px;color:var(--dim);font-size:12px}
.grp{align-items:center;gap:4px;padding:2px 0;border-top:1px solid rgba(255,255,255,.06)}
.grp-l{flex:0 0 92px;color:var(--sel);font-size:11px}
/* **flex は .withdesc に掛ける**。.withdesc は縦方向 flex なので、その子の .cb に
   flex-basis を指定すると「幅」ではなく「高さ」になり箱が縦長になる (2026-07-28 実害)。 */
.grp>.withdesc{flex:1 1 200px;min-width:0}
.grp>.withdesc>.cb{width:100%}
.tag .cb{margin-left:4px}
.tag .cb-btn{padding:0 4px;font-size:11px}
.sub-row{margin:-2px 0 6px;font-size:12px;color:var(--dim)}
.sub-row .cb{flex:0 1 200px}
.cb-pop,.cb-pop *{text-shadow:0 1px 2px rgba(0,0,0,.95),0 0 6px rgba(0,0,0,.85)}
#panel,#lpanel{overflow-x:hidden}
#panel .cb,#panel input:not([type=checkbox]),#panel textarea,
#lpanel .cb,#lpanel input:not([type=checkbox]),#lpanel textarea{max-width:100%}
#panel .row,#lpanel .row{max-width:100%}
#panel .withdesc,#lpanel .withdesc{max-width:100%;flex:1 1 auto}
/* 関係の行は 5 個の select が並ぶので、狭いときは折り返して溢れさせない */
#rels .row>.cb{flex:1 1 150px;min-width:0}
/* 部位から作った関係の主語 — 選択欄ではなく確定した事実として見せる */
/* 主語のチップも人物の色にする (緑固定だと誰の関係か色で判らない) */
.subj-fix{border-color:var(--c,var(--acc));color:var(--c,var(--acc))}
/* 関係 1 件のブロック。左の帯が主語の人物の色 = 誰の関係かが色で分かる。
   主語が未設定なら灰色 (色は人物の identity なので、無い人物に色を割り当てない) */
.rel{border-left:4px solid var(--c,var(--line));padding:5px 0 5px 8px;margin:0 0 6px;
     background:rgba(255,255,255,.03);border-radius:0 4px 4px 0}
.rel+.rel{margin-top:0}
#rels .row>.withdesc{flex:1 1 200px;min-width:0}
.sub-row>.cb{flex:0 1 190px;min-width:0}
.grp-cell{flex:1 1 auto;min-width:0;display:flex;flex-direction:column;gap:3px}
.rg{display:flex;align-items:center;gap:5px;min-width:0;flex-wrap:wrap}
.rg>.withdesc{flex:1 1 180px;min-width:0}
/* `.rg .cb{width:100%}` だと「左右」や「＋属性」まで幅いっぱいになり、
   1 行で済むものが 3 行に落ちていた (2026-07-28 指摘)。**幅いっぱいにするのは
   説明付き (.withdesc) の主たる選択肢だけ**にする。 */
.rg>.withdesc>.cb{width:100%}
.rg>.cb{flex:0 1 auto;min-width:110px;max-width:220px}
.rg>.cb.lat{flex:0 0 84px;min-width:84px}
.a-line{padding-left:14px;border-left:2px solid rgba(95,214,138,.35);margin-left:3px}
.a-n{flex:0 0 auto;color:var(--acc);font-size:11.5px;min-width:104px}
.a-line .cb-btn{font-size:12px;padding:2px 5px}
/* pose は属性と同じ「部位カテゴリの子」だが別軸なので線の色を変える (緑=属性 / 青=pose) */
.p-line{padding-left:14px;border-left:2px solid rgba(122,168,240,.40);margin-left:3px}
/* 橙 = 部位に紐づかない属性 (observation では region を持たない) を、観る場所の箱に置いたもの */
.e-line{padding-left:14px;border-left:2px dashed rgba(240,160,80,.55);margin-left:3px}
.e-line .a-n{color:var(--warn)}
.e-line .cb-btn{font-size:12px;padding:2px 5px}
/* 多値属性のチェックボックス群 — 値が排他でないので 1 行に並べて同時に選べる */
.multi{align-items:flex-start}
.ckset{display:flex;flex-wrap:wrap;gap:2px 8px;flex:1 1 160px;min-width:0}
.ck{display:inline-flex;align-items:center;gap:3px;font-size:11.5px;color:var(--dim);
    cursor:pointer;white-space:nowrap}
.ck.on{color:var(--acc)}
.ck input{margin:0;accent-color:var(--acc)}
.p-line .a-n{color:var(--sel)}
.p-line .cb-btn{font-size:12px;padding:2px 5px}
/* ── bbox 描画モード ────────────────────────────────────────
   画像はパネルの**裏**まで広がっているので、通常時はそこをドラッグできない
   (2026-07-28 指摘)。モード中だけ画像を最前面へ出し、パネルは透過して触れなくする。 */
main{z-index:5}
body.bbm main{z-index:60}
body.bbm #panel,body.bbm #lpanel{opacity:.14;pointer-events:none}
body.bbm #stage img{cursor:crosshair;border-color:var(--acc);box-shadow:0 0 0 2px var(--acc)}
#peek{z-index:70}
#bbmbar{display:none;position:fixed;left:50%;transform:translateX(-50%);
        top:calc(var(--hh) + 8px);z-index:70;align-items:center;gap:10px;
        background:rgba(10,13,16,.88);border:1px solid var(--acc);border-radius:999px;
        padding:5px 14px;font-size:12.5px;color:var(--acc)}
body.bbm #bbmbar{display:flex}
button.bbm-on{background:#16301f;border-color:var(--acc);color:var(--acc)}
.withdesc{display:inline-flex;flex-direction:column;gap:2px;min-width:0}
.withdesc .cb{max-width:100%}
.desc{color:var(--acc);font-size:11.5px;line-height:1.35;max-width:100%;white-space:normal}
/* ── スクロールバー ─────────────────────────────────────────
   既定のバーは不透明な帯で、透過を上げたパネルの上では**一番濃い要素**になって
   画像を遮る (2026-07-29 指摘)。細く・半透明にし、触れているときだけ濃くする。
   Firefox は scrollbar-color、WebKit は ::-webkit-scrollbar で別々に指定が要る。 */
*{scrollbar-width:thin;scrollbar-color:rgba(160,180,210,.22) transparent}
*:hover{scrollbar-color:rgba(160,180,210,.40) transparent}
::-webkit-scrollbar{width:7px;height:7px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-corner{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(160,180,210,.20);border-radius:99px;
  border:2px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:rgba(160,180,210,.45);background-clip:padding-box}
/* nav は 1 行なのでバー自体を隠す (pill が見えていれば横に流せると分かる) */
#nav.navwrap{scrollbar-width:none}
#nav.navwrap::-webkit-scrollbar{height:0}

/* ══════════════════════════════════════════════════════════════════════════
   狭い画面 (スマホ / タブレット)。**横向きを前提にする** (user 判断 2026-07-30
   「横向き専用でないと作れないというのであれば横向きにしてもかまわない」)。
   基準は iPhone Pro Max 横 = 932x430。

   写真アプリの作法に寄せる (Lightroom mobile / Snapseed / Procreate):
     * キャンバス (画像) は**常に見えている**。コントロールで覆わない
     * コントロールは**片側 1 枚だけ**。切替はタブ
     * タップ対象は 44px 以上 (Apple HIG の最小サイズ)
     * ノッチ・ホームインジケータは safe-area-inset で避ける
   desktop の挙動は一切変えない — この block は狭いときだけ効く。
   ══════════════════════════════════════════════════════════════════════════ */
@media (max-width: 1100px){
  :root{--hh:84px; --mpw:min(46vw,430px)}      /* --mpw = 右パネルの幅 */
  /* 画像は**パネルと重ならない**領域に置く。覆う設計をやめるのが要点 */
  main{inset:var(--hh) var(--mpw) 0 env(safe-area-inset-left);padding:4px}
  #stage img{max-height:calc(100dvh - var(--hh) - 8px)}
  /* パネルは右の 1 枚だけ。左パネルの中身も同じ枠に入れて**タブで切り替える** */
  #panel,#lpanel{top:var(--hh);bottom:0;left:auto;
       right:env(safe-area-inset-right);width:var(--mpw);
       padding:8px 10px calc(28px + env(safe-area-inset-bottom));
       border-left:1px solid var(--line);border-right:none;
       background:linear-gradient(to left,rgba(12,14,18,.72),rgba(12,14,18,.52))}
  /* 表示するのは .mtab-on が付いた方だけ */
  #panel:not(.mtab-on),#lpanel:not(.mtab-on){display:none}
  /* header は詰める。説明文は panel 側に出さない (幅を食う) */
  header{gap:6px;padding:4px 8px;padding-left:calc(8px + env(safe-area-inset-left));
         padding-right:calc(8px + env(safe-area-inset-right))}
  header .hint,#peek{display:none}
  .postno{padding:1px 6px}
  /* 長い説明の段落は畳む — 電話では読むより触る */
  .m-narrow-hide{display:none}
  /* タップ対象を 44px に (Apple HIG)。desktop の密度は保ったまま */
  .cb-btn,#panel input,#panel textarea,#lpanel input,#lpanel textarea{
    min-height:38px;font-size:15px}
  .cb-i{min-height:44px;display:flex;align-items:center;gap:6px}
  .pill{min-height:30px;display:inline-flex;align-items:center}
  button.go,button.sub{min-height:34px}
  /* combobox の popup は**下から出す全幅シート** (iOS のピッカーの作法)。
     キーボードが出ても隠れないように JS が visualViewport で高さを詰める */
  .cb-pop{left:env(safe-area-inset-left)!important;
          right:env(safe-area-inset-right)!important;width:auto!important;
          background:rgba(14,17,22,.92)}
  .cb-search{min-height:40px;font-size:16px}   /* 16px 未満は iOS が自動ズームする */
  /* 横向きは高さが貴重なのでサムネイルを小さく */
  .th img{height:36px;max-width:64px}
  .th b{font-size:8px}
}
/* タブバー (狭いときだけ出す)。header の中に置くと 40px を食うので画像の上端に重ねる */
#mtabs{display:none}
@media (max-width: 1100px){
  #mtabs{display:flex;gap:4px;position:fixed;z-index:35;
         top:calc(var(--hh) + 4px);right:calc(var(--mpw) + 6px)}
  #mtabs button{min-height:32px;padding:2px 10px;font-size:12px;
                background:rgba(14,17,22,.82);color:var(--dim);
                border:1px solid var(--line);border-radius:999px}
  #mtabs button.on{color:var(--sel);border-color:var(--sel)}
}

/* ── 狭い画面での「重ねる」モード (user 判断 2026-07-30「透明化で対応してもいいよ」) ──
   並べると画像が 490px に痩せる。細部 (乳輪の広さ・precum・陰茎の形) は大きさが
   そのまま判定精度なので、**画像を全幅にしてパネルを半透明で重ねる**方を選べるようにする。
   desktop が最初からこの方式 — 同じ作法を狭い画面にも持ち込むだけ。
   タッチには hover が無いので、濃さは**明示的なボタンで 3 段**切り替える。 */
@media (max-width: 1100px){
  body.m-overlay main{inset:var(--hh) 0 0 env(safe-area-inset-left)}
  body.m-overlay #panel,body.m-overlay #lpanel{
    background:linear-gradient(to left,rgba(12,14,18,.60),rgba(12,14,18,.34))}
  /* 薄 */
  body.m-overlay.m-dim1 #panel,body.m-overlay.m-dim1 #lpanel{
    background:linear-gradient(to left,rgba(12,14,18,.34),rgba(12,14,18,.14))}
  body.m-overlay.m-dim1 #panel section,body.m-overlay.m-dim1 #lpanel section{
    background:rgba(20,23,28,.16)}
  /* 透 — 位置合わせの瞬間だけ。文字は影で拾う */
  body.m-overlay.m-dim2 #panel,body.m-overlay.m-dim2 #lpanel{background:none}
  body.m-overlay.m-dim2 #panel section,body.m-overlay.m-dim2 #lpanel section{background:none}
  body.m-overlay.m-dim2 #panel .cb-btn,body.m-overlay.m-dim2 #panel input,
  body.m-overlay.m-dim2 #lpanel .cb-btn,body.m-overlay.m-dim2 #lpanel input{
    background:rgba(10,13,16,.30)}
}

/* ── 上部のサムネイルリスト (0049)。番号 pill を置き換えた ──
   状態は枠の色で出す (確定=緑 / 下書き=橙 / 未着手=灰)。現在地は外側の輪郭。
   高さは --hh に連動するので、main / #panel / #lpanel は自動で追従する。 */
#nav.navwrap{align-items:center;gap:4px}
.th{position:relative;flex:0 0 auto;display:block;cursor:pointer;line-height:0;
    border:2px solid var(--line);border-radius:4px;overflow:hidden;
    background:#0f1216;opacity:.72;transition:opacity .1s}
.th:hover{opacity:1}
.th img{display:block;height:52px;width:auto;max-width:96px;object-fit:cover}
.th b{position:absolute;left:0;right:0;bottom:0;font-size:9px;font-weight:600;
      line-height:1.3;text-align:center;color:#dfe3ea;background:rgba(8,10,13,.72);
      letter-spacing:.02em}
.th.st-done{border-color:var(--acc)}
.th.st-draft{border-color:var(--warn)}
.th.st-todo{border-color:var(--line)}
.th.cur{opacity:1;outline:2px solid var(--sel);outline-offset:1px}
/* 層D は utility の分子に入らないので薄くする (落とすのではなく順序) */
.th.later{opacity:.38}
.th.later:hover{opacity:1}
</style></head><body>
<header>
  <b>kura1 注釈</b>
  <a id="curpost" target="_blank" rel="noopener"
     title="いま開いている post 番号。クリックで kura の該当ページを開く">—</a>
  <span id="prog" class="hint"></span>
  <button class="sub" onclick="nextTodo()" title="層の順に次の未着手へ">次へ</button>
  <!-- 0049: **post 番号で飛べるようにする。** pill は通し番号しか出ないので、
       76 個の中から特定の post を探すのが実質できなかった (user 指摘 2026-07-31)。
       URL の ?post= / #post でも開ける — 会話やメモから直接リンクできる。 -->
  <input id="jump" type="text" inputmode="numeric" placeholder="post 番号"
         title="post 番号を入れて Enter で移動" style="width:96px">
  <span id="nav" class="row navwrap"></span>
  <span style="flex:1"></span>
  <button id="bbmbtn" class="sub" onclick="setBbm(!document.body.classList.contains('bbm'))"
          title="B キー">位置を指す</button>
  <label style="margin:0" title="画面を見ている時間から自動で入ります。手で直せます">分
    <input id="minutes" type="number" step="0.1" style="width:64px"
           oninput="minutesTouched=true"></label>
  <span id="st" class="hint">—</span>
  <span id="ready" class="hint"></span>
  <button id="gobtn" class="go" onclick="commitAndNext()">確定して次へ</button>
  <span id="stale" class="stale" hidden></span>
  <span id="cerr" class="stale" hidden></span>
  <!-- 表示は長辺 1800px の WebP。細部 (乳輪の広さ・precum 等) は**原寸で見る** -->
  <a id="orig" class="sub" href="#" target="_blank" rel="noopener"
     title="表示は長辺 1800px に縮小されています。細部の判断は原寸で">原寸</a>
</header>
<div id="bbmbar"><b id="bbmwho">—</b>
  <span>の</span><span id="bbmpart"></span>
  <span>を<b>クリック</b> / ドラッグすると bbox</span>
  <button class="sub" onclick="setBbm(false)">終了 (Esc)</button></div>
<main>
  <div id="stage">
    <img id="im" alt="" draggable="false">
    <div id="boxes"></div>
  </div>
</main>
<button id="peek" class="sub" onclick="['panel','lpanel'].forEach(i=>document.getElementById(i).classList.toggle('hide'))">隠す</button>
<div id="lpanel">
    <section><h3>この画像について</h3>
      <div class="hint m-narrow-hide"><b>位置を指す</b> (または <b>B</b> キー) で指定モード —
        <b>クリックで頭の中心</b>、<b>ドラッグで bbox</b>。画像が最前面に出るのでパネルの裏側も指せます。
        指し終えると自動で戻ります。人物カードをクリックで選択切替。<br>
        <b>1 人だけの画像では位置は要りません</b> — 画面全体が主体なので情報になりません。
        複数人のときだけ、誰の事実かを画素で言うために要ります。</div>
      <div id="warn" class="hint"></div>
    </section>
    <section><h3>語を探す</h3>
      <div class="hint"><b>どのコントロールに居るか判らなくても引けます</b> —
        <b id="vcount">全語彙</b>を
        まとめて検索し、<b>どこに属する語か</b>と<b>選択中の人物へ入れるボタン</b>を出します。
        slug でも日本語でも説明文でも引けます。</div>
      <div class="row"><input id="find" placeholder="泡 / dogeza / 膣 / 排尿"
        style="flex:1 1 260px"><button class="sub" onclick="runFind()">探す</button></div>
      <div id="findout"></div></section>
    <section><h3>文で入れる</h3>
      <div class="hint"><code>p1 vaginal sex p2 from behind</code> のように書くと、
        <b>人が書いた展開の型</b>が当たってコントロールが埋まります。
        <b>DB には何も書きません</b> — 見て直して、いつもどおり確定してください。
        <b>解釈できなかった語は下に赤で出ます</b> (黙って捨てません)。</div>
      <div class="row"><b class="k">主体</b><span id="saywho"></span>
        <input id="say" placeholder="p1 vaginal sex p2 from behind"
        style="flex:1 1 200px" title="Enter で適用"><button class="sub" onclick="applySay()">適用</button>
        <button class="sub" onclick="sayHelp()">型の一覧</button></div>
      <div id="sayout" class="hint"></div></section>
    <section><h3>噴出物 (何が / どこから / どこへ)</h3>
      <div class="hint"><b>出どころは器官に縛りません</b> — 膣口から溢れる精液、口から溢れる便も書けます (0062)。語が言うのは<b>物質であって産出者ではない</b>ので、誰が出したかは別の所見で言います。</div>
      <label>出ているか (人物ごと)</label><div id="emis-a"></div>
      <label>どこから どこへ</label><div id="emis-r"></div>
      <button class="sub" onclick="addEmisRel()">＋噴出物の行き先</button></section>
    <section><h3>関係 (何が / 誰の何に)</h3>
      <div class="hint">主語 (誰の・どこ) は<b>人物側の部位の「＋関係」</b>から作れば自動で入ります。ここで作るのは部位に紐づかない関係 (人物どうしの位置関係など)。</div>
      <div id="rels"></div>
      <div id="relobs"></div>
      <button class="sub" onclick="addRel()">＋関係を追加</button></section>
    <section><h3>画面全体</h3>
      <div id="derived" class="hint"></div>
      <label>カメラ・構図 (scene_descriptor)</label><div id="descs" class="row"></div>
      <div id="descobs"></div>
      <label>人数 (entity_count) — <b>背景の観衆は「見ているだけ」+ 正確な人数を外す</b></label><div id="counts"></div>
      <button class="sub" onclick="addCount()">＋人数を追加</button></section>
    <section><h3>特定できないもの (unresolved)</h3>
      <div class="hint">どの人物のものか判別できない事実はここへ。<b>推測でどちらかに付けないでください。</b></div>
      <div id="unres"></div><button class="sub" onclick="addUnres()">＋追加</button></section>
    <section><h3>書けなかったこと <span id="gapn" class="hint"></span></h3>
      <div class="hint">語彙・スキーマに無くて書けなかったものをここに。
        <b>その場で止まらず、書いて次へ進んでください</b> — 後でまとめて設計します。</div>
      <textarea id="gaps" placeholder="例: 触手が書けない / 蹲踞に当たる pose が無い"></textarea></section>
    <section><h3>メモ</h3><textarea id="notes" placeholder="ref の振り方、その scene 固有の注意など"></textarea></section>
    <section><h3>確定済みの所見 <span class="hint">— <b class="k pd-model-t">モデル</b> と <b class="k pd-human-t">人間</b> を色分け</span></h3>
      <div class="hint">この scene で <b>DB に確定済み</b>の所見。上の下書き (作業中) とは別。arm M ではモデルの所見がここに出るので、誤りは撤回し欠落は下書きに足します。</div>
      <div id="obsreview" class="hint">—</div></section>
    <section><h3>結果</h3><div id="msg" class="hint">未検証</div></section>
</div>
<div id="panel">
    <section><h3>人物</h3><div id="subjects"></div>
      <button class="sub" onclick="addSubj()">＋人物を追加</button>
      <button class="sub" onclick="proposeSubjects()" id="propbtn"
        title="顔検出の結果を人物として足します。既にある人物は消しません">機械提案を取り込む</button>
      <div id="propmsg" class="hint"></div></section>
</div>
<script>
const KURA_ORIGIN = __KURA_ORIGIN__;

// ══════════════════════════════════════════════════════════════════════════
// **API の基点をページの位置から導く。** 将来 kura の 1 モードとして
// `/annotate/` のような prefix の下にマウントできるようにするための準備
// (user 指示 2026-07-30「今後 kura のモードの一つにできるように準備しておくこと」)。
// 絶対パス (`/api/vocab`) を直書きしていると prefix の下では 404 になる。
// ページが `/x/y/` から配られたら基点は `/x/y` — server 側の設定は要らない。
// 詳細な統合手順は database/ANNOTATE-UI-INTEGRATION.md
// ══════════════════════════════════════════════════════════════════════════
const API = location.pathname.replace(/\/[^/]*$/, '');
const U = path => API + path;      // U('/api/vocab') → '/annotate/api/vocab'


// 「並べる / 重ねる」と濃さ 3 段。**どちらも localStorage に残す** — 毎回選び直すのは
// 作業の邪魔になる。desktop では body class が無効なので何もしない。
function setMOverlay(on){
  document.body.classList.toggle('m-overlay', !!on);
  try{ localStorage.setItem('kura1.movl', on?'1':'0') }catch(e){}
  const b=document.querySelector('#mtabs button[data-t="__ovl"]');
  if(b){ b.classList.toggle('on', !!on); b.textContent = on?'重ねる':'並べる' }
}
function setMDim(level){                    // 0=濃 1=薄 2=透
  const b=document.body;
  b.classList.toggle('m-dim1', level===1); b.classList.toggle('m-dim2', level===2);
  try{ localStorage.setItem('kura1.mdim', String(level)) }catch(e){}
  const btn=document.querySelector('#mtabs button[data-t="__dim"]');
  if(btn) btn.textContent = ['濃','薄','透'][level];
}
function mDim(){ let v=0; try{ v=+(localStorage.getItem('kura1.mdim')||0) }catch(e){} return v }


// ══════════════════════════════════════════════════════════════════════════
// 狭い画面 (スマホ横) 対応。**desktop には一切触らない** — matchMedia が偽なら
// 何もしない。写真アプリの作法: キャンバスを覆わない / パネルは 1 枚 / タブで切替。
// ══════════════════════════════════════════════════════════════════════════
const MQ = matchMedia('(max-width: 1100px)');
function mobileTabs(){
  let bar=document.getElementById('mtabs');
  if(!bar){
    bar=el('div',{},''); bar.id='mtabs';
    const mk=(label,target)=>{
      const b=el('button',{type:'button'},label);
      b.addEventListener('click',()=>setMTab(target));
      b.dataset.t=target; return b};
    bar.append(mk('人物','panel'), mk('画面・関係','lpanel'));
    // 並べる/重ねる と 濃さ。**画像の大きさは判定精度に直結する**ので上げられるようにする
    const ov=el('button',{type:'button'},'並べる'); ov.dataset.t='__ovl';
    ov.addEventListener('click',()=>setMOverlay(!document.body.classList.contains('m-overlay')));
    const dm=el('button',{type:'button'},'濃'); dm.dataset.t='__dim';
    dm.addEventListener('click',()=>setMDim((mDim()+1)%3));
    bar.append(ov,dm);
    document.body.append(bar);
  }
  return bar;
}
function setMTab(which){
  const bar=mobileTabs();
  ['panel','lpanel'].forEach(id=>{
    const p=document.getElementById(id);
    if(p) p.classList.toggle('mtab-on', id===which);
  });
  [...bar.children].forEach(b=>b.classList.toggle('on', b.dataset.t===which));
  try{ localStorage.setItem('kura1.mtab', which) }catch(e){}
}
function applyMode(){
  if(MQ.matches){
    mobileTabs();
    let want='panel';
    try{ want=localStorage.getItem('kura1.mtab')||'panel' }catch(e){}
    setMTab(want);
    let ovl='0'; try{ ovl=localStorage.getItem('kura1.movl')||'0' }catch(e){}
    setMOverlay(ovl==='1'); setMDim(mDim());
  }else{
    // desktop に戻ったら両方出す (mtab-on は狭いときだけ意味を持つ)
    ['panel','lpanel'].forEach(id=>{
      const p=document.getElementById(id); if(p) p.classList.remove('mtab-on')});
    const bar=document.getElementById('mtabs'); if(bar) bar.remove();
  }
}
MQ.addEventListener('change', applyMode);
// **visualViewport で popup の高さを詰める。** iOS のソフトキーボードは innerHeight を
// 変えないので、innerHeight 基準だと popup がキーボードの下に隠れる (MDN VisualViewport)。
// vv.height + vv.offsetTop が「実際に見えている下端」。
function vvBottom(){
  const vv=window.visualViewport;
  return vv ? vv.height + vv.offsetTop : innerHeight;
}
if(window.visualViewport){
  const re=()=>{const p=document.querySelector('.cb-pop'); if(p&&p.__place)p.__place()};
  visualViewport.addEventListener('resize',re);
  visualViewport.addEventListener('scroll',re);
}

let V={}, WL=[], cur=null, ann=null, act=0, DERIVED=null;
const $=id=>document.getElementById(id);
const el=(t,a={},...c)=>{const n=document.createElement(t);
  for(const[k,v]of Object.entries(a)){if(k=='on')Object.entries(v).forEach(([e,f])=>n.addEventListener(e,f));
  else if(k=='cls')n.className=v;else n.setAttribute(k,v)} c.flat().forEach(x=>n.append(x)); return n};
// list は ['slug',...] でも [{slug,d,g},...] でもよい。
// **ネイティブ <select> のポップアップは CSS が効かず不透明で画像を隠す**ので
// (2026-07-28 指摘)、自前の combobox にする。副産物として **日本語で絞り込める** —
// 117 語の部位を「うなじ」と打って引ける。
const norm=l=>l.map(x=>typeof x==='string'?{slug:x,d:'',g:''}:x);
let openCb=null;
function opt(list,sel,ph){
  const L=norm(list);
  const root=el('span',{cls:'cb'});
  const btn=el('button',{cls:'cb-btn',type:'button'});
  const pop=el('div',{cls:'cb-pop'});
  const sIn=el('input',{cls:'cb-search',placeholder:'絞り込み (日本語可)'});
  const ul=el('div',{cls:'cb-list'});
  pop.append(sIn,ul);
  let value=sel||'', items=[], cursor=-1;
  const cur=()=>L.find(v=>v.slug===value);
  // n = 表示名 (無ければ slug)。属性+値をひとつの項目にするとき、値は slug と
  // 別の合成キー ("breast_size\u241Fhuge") になるので、見せる文字列を分ける必要がある。
  const nm=v=>v.n||v.slug;
  function paint(){const m=cur();
    btn.replaceChildren();
    // 項目が色を持つなら**選択後も**その色で見せる。人物の参照は色が identity なので、
    // 選んだ瞬間に色が消えると画像上の印と突き合わせられなくなる (2026-07-29 指摘)。
    if(m&&m.c) btn.append(el('em',{cls:'dot',style:`--c:${m.c}`}));
    // **説明文を全文入れない。** 入れていた版では `creature` の説明 (200 字) が
    // ボタンに丸ごと入り、nowrap の intrinsic width が巨大になって header の
    // flex が折り返し、**カードが崩れた** (user 指摘 2026-07-30
    // 「creature を選択すると ui がつぶれる」)。説明は title と popup に既にある。
    // 1 文目だけ、しかも 36 字で切る — 語彙の説明がいくら長くなっても崩れない。
    const shortD = d => { const s=String(d).replace(/\*\*/g,'').split(/[。\n]/)[0].trim();
                          return s.length>36 ? s.slice(0,36)+'…' : s };
    btn.append(m?(m.d?`${nm(m)} — ${shortD(m.d)}`:nm(m)):(ph||'—'));
    btn.style.color = m&&m.c ? m.c : '';
    btn.classList.toggle('empty',!m); btn.title=m?mdPlain(m.d||nm(m)):'';
    const w=root.closest('.withdesc');
    if(w){const d=w.querySelector('.desc');
          if(d) d.replaceChildren(m&&m.d?mdFrag(m.d):'')}}
  function build(q){
    ul.replaceChildren(); items=[]; cursor=-1;
    const qq=(q||'').trim().toLowerCase();
    // **slug も検索対象にする。** 表示名が日本語のとき (species) 英字 slug で
    // 引けなくなる。逆に slug だけだと日本語で引けない — 両方見るのが正解。
    const hit=L.filter(v=>!qq||nm(v).toLowerCase().includes(qq)
                          ||String(v.slug||'').toLowerCase().includes(qq)
                          ||(v.d||'').toLowerCase().includes(qq)
                          ||(v.g||'').toLowerCase().includes(qq));
    // **グループごとにまとめてから描く**。項目が slug 順に並んでいても見出しが
    // 繰り返し出ないようにする (2026-07-28 実害: 見出しが 80 回出た)。
    const order=[], byg=new Map();
    hit.forEach(v=>{const g=v.g||''; if(!byg.has(g)){byg.set(g,[]);order.push(g)} byg.get(g).push(v)});
    order.forEach(g=>{
      if(g) ul.append(el('div',{cls:'cb-g',title:mdPlain(g)},mdFrag(g)));
      byg.get(g).forEach(v=>{
        const it=el('div',{cls:'cb-i'+(v.slug===value?' on':'')},
          v.c?el('em',{cls:'dot',style:`--c:${v.c}`}):'',
          el('b',{style:v.c?`color:${v.c}`:''},nm(v)),
          v.d?el('i',{},mdFrag(v.d)):'');
        it.addEventListener('mousedown',e=>{e.preventDefault(); pick(v.slug)});
        ul.append(it); items.push(it)})});
    if(!items.length) ul.append(el('div',{cls:'cb-none'},'該当なし'));
  }
  // **広い側に開く。** `below>240` の固定しきい値だった版は、下に 250px しか無いときも
  // 下向きに開いて **5 項目ほどで切れていた** (user 指摘 2026-07-30
  // 「画面下のドロップダウンリストが殆ど表示できない」)。上に 800px 空いていても
  // 下を選ぶので、画面下部のコントロールが実質使えなかった。
  // 規則: 下に十分 (>=320px) あればそのまま下 — 開く方向が跳ねないため。
  //       足りないときは**広い方**を選ぶ。
  function place(){const r=btn.getBoundingClientRect();
    // **幅を決めてから左を決める。** 逆にしていた版は left を innerWidth-360 で
    // 抑えるだけで、幅が 520 まで伸びると**右へ 160px はみ出して読めなかった**
    // (user 指摘 2026-07-31、右パネルは欄が広いので必ずこうなる)。
    const w=Math.max(320,Math.min(Math.max(r.width,360),560),0);
    pop.style.width=Math.min(w,innerWidth-12)+'px';
    pop.style.left=Math.max(6,Math.min(r.left,innerWidth-6-Math.min(w,innerWidth-12)))+'px';
    // **見えている下端は innerHeight ではない。** iOS のソフトキーボードは
    // innerHeight を変えないので、それ基準だと popup がキーボードの下に隠れる
    // (MDN VisualViewport: vv.height + vv.offsetTop が実際の下端)。
    const vb=vvBottom();
    const below=vb-r.bottom, above=r.top;
    const useBelow = below>=320 || below>=above;
    if(useBelow){pop.style.top=(r.bottom+3)+'px';pop.style.bottom='auto';
                 pop.style.maxHeight=Math.max(140,below-14)+'px'}
    else{pop.style.bottom=(vb-r.top+3)+'px';pop.style.top='auto';
         pop.style.maxHeight=Math.max(140,above-14)+'px'}}
  pop.__place=place;      // visualViewport の resize/scroll から呼び直せるように
  function open(){if(openCb&&openCb!==close)openCb(); openCb=close;
    build(''); sIn.value=''; document.body.append(pop); place(); sIn.focus()}
  function close(){if(pop.parentNode)pop.remove(); if(openCb===close)openCb=null}
  function pick(v){value=v; close(); paint(); root.dispatchEvent(new Event('change',{bubbles:true}))}
  btn.addEventListener('click',e=>{e.stopPropagation(); pop.parentNode?close():open()});
  sIn.addEventListener('input',()=>build(sIn.value));
  sIn.addEventListener('keydown',e=>{
    if(e.key==='Escape'){close();btn.focus()}
    else if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();
      if(!items.length)return; cursor=(cursor+(e.key==='ArrowDown'?1:-1)+items.length)%items.length;
      items.forEach((n,i)=>n.classList.toggle('cur',i===cursor));
      items[cursor].scrollIntoView({block:'nearest'})}
    else if(e.key==='Enter'){e.preventDefault();
      const n=items[cursor>=0?cursor:0]; if(n)pick(n.querySelector('b').textContent)}});
  addEventListener('click',()=>{if(pop.parentNode)close()});
  addEventListener('scroll',()=>{if(pop.parentNode)place()},true);
  Object.defineProperty(root,'value',{get:()=>value,set:v=>{value=v||'';paint()}});
  root.append(btn); paint(); return root;
}
// 語彙の説明は **markdown で書いてある** (migration がそう書いている)。
// それを textContent に入れていたので `**` が画面にそのまま出ていた
// (user 指摘 2026-07-31)。**innerHTML は使わない** — 説明は DB から来る文字列で、
// 素通しすると語彙を足す migration が UI に HTML を注入できてしまう。
// 必要なのは強調と code の 2 つだけなので、自前で刻んで要素にする。
function mdFrag(s){
  const f=document.createDocumentFragment();
  String(s==null?'':s).split(/(\*\*[^*]+\*\*|`[^`]+`)/).forEach(part=>{
    if(!part) return;
    if(part.startsWith('**')&&part.endsWith('**')&&part.length>4)
      f.append(el('b',{},part.slice(2,-2)));
    else if(part.startsWith('`')&&part.endsWith('`')&&part.length>2)
      f.append(el('code',{},part.slice(1,-1)));
    else f.append(part);
  });
  return f;
}
// title 属性は素の文字列しか持てないので**印だけ落とす**
const mdPlain=s=>String(s==null?'':s).replace(/\*\*/g,'').replace(/`/g,'');
const withdesc=s=>el('span',{cls:'withdesc'},s,el('div',{cls:'desc'}));

// 開いていた scene を憶えておく。リロードで毎回先頭 (層A の 1 枚目) に飛ばされると
// 作業中の 1 枚に戻るのに探す手間がかかる (2026-07-29 指摘)。
// **localStorage に置く** — これは画面の状態であって注釈の内容ではないので、
// 下書き (サーバ側) とは別の場所に持つ。DB にも下書き JSON にも混ぜない。
const LAST_KEY='kura1.annotate.lastPost';
function rememberPost(pid){ try{ localStorage.setItem(LAST_KEY, String(pid)) }catch(e){} }
function urlPost(){
  // ?post=648990 / #648990 のどちらでも受ける。**URL が localStorage より優先** —
  // リンクを踏んだのに前回の続きが開くのは意図に反する。
  const m = new URLSearchParams(location.search).get('post')
         || (location.hash||'').replace(/^#/,'');
  const n = parseInt(m||'', 10);
  return Number.isFinite(n) ? n : null;
}
function gotoPost(pid){
  if(!WL.some(w=>w.post_id===pid)){
    const j=document.getElementById('jump');
    if(j){ j.style.borderColor='var(--err)'; setTimeout(()=>j.style.borderColor='',900) }
    return false;
  }
  load(pid); return true;
}
function recallPost(){
  const u=urlPost();
  if(u && WL.some(w=>w.post_id===u)) return u;
  let saved=null;
  try{ saved=parseInt(localStorage.getItem(LAST_KEY)||'',10) }catch(e){}
  // 憶えていた scene が worklist に無い (別 DB / 対象が変わった) なら捨てて
  // 未着手の先頭へ。存在しない post を開こうとして固まらないようにする。
  // localStorage の記憶も **arm フィルタ内**でだけ使う (?arm=H で M の記憶を開かない)
  if(saved && WL.some(w=>w.post_id===saved && armOk(w))) return saved;
  // arm フィルタ中はその arm の未着手を先頭に。無ければ arm 内の先頭
  return (WL.find(w=>w.state!=='done'&&armOk(w))
          || WL.find(w=>armOk(w)) || WL[0]).post_id;
}
// **?ro=1 は下書きを一切保存しない。** ブラウザを機械で動かすと、注釈者が編集中の
// 下書きを黙って上書きしてしまう (2026-07-28 に一度、2026-07-31 に再発 —
// 555215 の編集中に検査が関係 2 本と部位 1 つを混入させた)。
// ANN_DIR を分ける方法はサーバ起動時に決まるので、**注釈者が使っている
// サーバに繋ぐ検査には効かない**。効くのは client 側のこの門だけ。
const RO=new URLSearchParams(location.search).get('ro')==='1';
// arm フィルタ (?arm=H / M)。round 4 の二腕化 (fact 6eadd66f) を注釈時に辿るため。
// H = 純人手、M = モデル先行 + 人間チェック。空なら全部。
function urlArm(){ return (new URLSearchParams(location.search).get('arm')||'').toUpperCase() }
function armOk(w){ const a=urlArm(); return !a || (w.arm||'')===a }
function urlPool(){
  const n=parseInt(new URLSearchParams(location.search).get('pool')||'',10);
  return Number.isFinite(n) ? n : null;
}
async function boot(){
  V=await (await fetch(U('/api/vocab'))).json();
  const pool=urlPool();
  if(pool){
    // **未 import なら先に skeleton を作る。** 空の一覧を見せてから
    // 「import して」と言わせるのは手順を増やすだけ。
    $('prog').textContent='pool '+pool+' を取り込み中…';
    try{ await (await fetch(U('/api/pool_import/')+pool)).json() }catch(e){}
  }
  await reloadWL();
  if(pool && !WL.length){
    $('prog').innerHTML='<span class="e">pool '+pool+' に該当する scene がありません</span>';
    return;
  }
  drawNav(); await load(recallPost());
  applyMode();          // 狭い画面ならタブを出してパネルを 1 枚にする
  const j=document.getElementById('jump');
  if(j){
    j.addEventListener('keydown',e=>{
      if(e.key!=='Enter') return;
      const n=parseInt(j.value.trim(),10);
      if(Number.isFinite(n) && gotoPost(n)) j.value='';
    });
    // 数字を入れた瞬間に候補があるか見せる (無い番号で Enter を押す無駄を減らす)
    j.addEventListener('input',()=>{
      const n=parseInt(j.value.trim(),10);
      j.style.color = (!j.value.trim() || (Number.isFinite(n) && WL.some(w=>w.post_id===n)))
                      ? '' : 'var(--err)';
    });
  }
  // **hashchange では hash を直接読む。** urlPost() は ?post= を先に見るので、
  // クエリが残っていると hash を変えても同じ post に戻ってしまう (2026-07-31 実測)。
  addEventListener('hashchange',()=>{
    const n=parseInt((location.hash||'').replace(/^#/,''),10);
    if(Number.isFinite(n)) gotoPost(n);
  });
}
// 58 件を **状態つき**で並べる。todo=未着手 / draft=下書きあり / done=確定済。
// 対象は DB に入っている scene 全部 (下書きファイルの有無は状態であって対象の定義ではない)。
function drawNav(){
  const n={todo:0,draft:0,done:0};
  WL.forEach(w=>n[w.state]++);
  const pl=urlPool();
  const arm=urlArm();
  const armLink=(a,lbl)=>{const u=new URLSearchParams(location.search);
    if(a)u.set('arm',a);else u.delete('arm');
    return `<a href="?${u}" class="armf${arm===a?' on':''}">${lbl}</a>`};
  $('prog').innerHTML =
    (pl?`<b class="k">pool ${pl}</b> ／ `:'')
    + `<b class="k">確定 ${n.done}</b> / 下書き ${n.draft} / 未着手 ${n.todo} <span class="hint">— 全 ${WL.length}</span>`
    + ` ／ ${armLink('','全て')} ${armLink('H','arm H 純人手')} ${armLink('M','arm M ﾓﾃﾞﾙ')}`;
  // **番号ではなくサムネイルで並べる** (user 指摘 2026-07-31「上部の番号リストは
  // 全くよくない。シングルビューでやっているようなサムネイルリストにして」)。
  // 番号だけでは 76 個の中から目的の画像を探せず、1 つずつ hover するしかなかった。
  // 画像は ?w= の縮小版を使う (原寸を 76 枚読むと数百 MB になる)。
  const shown=WL.filter(w=>!arm||(w.arm||'')===arm);
  $('nav').replaceChildren(...shown.map((w)=>{
    const i=WL.indexOf(w);
    const im=el('img',{src:U('/img/')+w.post_id+'?w=96', loading:'lazy', alt:''});
    // arm バッジ (H=青 / M=橙)。producer 別の所見数も点で出す
    const badge = w.arm ? el('em',{cls:'arm arm-'+w.arm, title:w.arm==='H'?'純人手':'モデル先行+人間チェック'}, w.arm) : '';
    const dots = [];
    if(w.n_model) dots.push(el('i',{cls:'pd pd-model', title:`モデルの所見 ${w.n_model} 件`}));
    if(w.n_human) dots.push(el('i',{cls:'pd pd-human', title:`人間の所見 ${w.n_human} 件`}));
    return el('span',
      {cls:'th st-'+w.state+(w.post_id==cur?' cur':'')+(w.stratum==='D'?' later':''),
       title:`${i+1}. post ${w.post_id} [層${w.stratum}]`
              +(w.arm?` [arm ${w.arm}]`:'')+' — '
              +(w.state==='done'?`確定済 (所見 ${w.written} 件)`
               :w.state==='draft'?'下書きあり (未確定)':'未着手')
              +(w.n_model?` ／ モデル ${w.n_model} 件`:''),
       on:{click:()=>load(w.post_id)}}, im, badge,
       el('span',{cls:'pdrow'},...dots), el('b',{},String(w.post_id)));
  }));
  // **どの post を見ているかは見出しにも出す** — サムネイルだけだと
  // 「397108 を見て」と言われたとき照合しにくい (2026-07-29 指摘)。
  const i=WL.findIndex(w=>w.post_id==cur), w=WL[i]||{};
  const a=$('curpost');
  a.textContent = cur ? `post ${cur}` : '—';
  a.href = cur && KURA_ORIGIN ? `${KURA_ORIGIN}/post/${cur}` : '#';
  a.className = 'postno' + (w.state ? ' st-'+w.state : '');
  a.title = cur ? `${i+1} / ${WL.length} — ` + (w.state==='done'?`確定済 (所見 ${w.written} 件)`
              : w.state==='draft'?'下書きあり (未確定)':'未着手')
              + '。クリックで kura の該当ページを開く' : '';
  const c=$('nav').querySelector('.cur'); if(c) c.scrollIntoView({block:'nearest',inline:'center'});
}
// 未着手のうち先頭 (層の順 = A→B→C→E→D) へ飛ぶ。埋める順を考えなくて済むように。
// **一覧の取り直しは 1 か所だけ。** 確定後の取り直しが `?pool=` を落としていたので、
// pool 32 件の一覧が全 108 件に化けていた (user 指摘 2026-07-31「確定して次へで
// 画像が切り替わらない」の原因の 1 つ)。同じ URL を 2 か所に書かない。
async function reloadWL(){
  const pl=urlPool();
  WL=await (await fetch(U('/api/worklist')+(pl?('?pool='+pl):''))).json();
  return WL;
}
function nextTodo(from){
  // **今の位置より後ろから探す。** 常に先頭から探していた版は、下書きが前に残って
  // いると同じ scene に戻り続けて**画像が変わらない**ように見えた。
  // 後ろに無ければ先頭へ回り、それでも今と同じなら「もう無い」と言う。
  const i=from==null?-1:WL.findIndex(x=>x.post_id==from);
  // arm フィルタ中は**その arm の scene だけ**を辿る (user 指摘 2026-07-31)
  const todo=x=>(x.state==='todo'||x.state==='draft')&&armOk(x);
  const w=WL.slice(i+1).find(todo) || WL.find(todo);
  if(w && w.post_id!=from) load(w.post_id);
  else $('msg').textContent='未着手・下書きはありません';
}
async function load(pid){
  if(ann && dirty){ clearTimeout(saveT); await draft(); }   // 切替で編集を落とさない
  cur=pid; rememberPost(pid);
  ann=await (await fetch(U('/api/ann/')+pid)).json();
  if(!ann.kura_post_id) ann={kura_post_id:pid,annotator:'desk',minutes:null,subjects:[],
     relations:[],scene_descriptors:[],entity_counts:[],unresolved:[],notes:''};
  ann.subjects=ann.subjects||[]; ann.relations=ann.relations||[];
  ann.scene_descriptors=ann.scene_descriptors||[]; ann.entity_counts=ann.entity_counts||[];
  ann.unresolved=ann.unresolved||[];
  // 属性を部位の下に置く前に書かれた下書きは region を持たない。applies_kind='region'
  // の属性は **ontology が対象部位を決めている**ので、そこから埋めるのは捏造ではない。
  // (これをしないと breast_size が「人物全体の属性」の箱に取り残されて見つからない)
  ann.subjects.forEach(s=>(s.attributes||[]).forEach(a=>{
    if(a.region) return;
    const m=V.attribute.find(x=>x.slug===a.attribute);
    // **その部位を「見えている」に挙げている場合だけ**移す。挙げていないのに移すと
    // 部位チップの下という表示先が無く、画面から消えてしまう (人物全体の箱に残す)。
    if(m&&m.applies_kind==='region'&&(s.regions||[]).includes(m.applies_key))
      a.region=m.applies_key;
  }));
  if(ann.subjects.length===0) addSubj(true);
  act=0; $('im').src=U('/img/')+pid+'?w=1800'; $('orig').href=U('/img/')+pid; $('minutes').value=ann.minutes??'';
  $('notes').value=ann.notes||''; $('gaps').value=ann.gaps||'';
  fetch(U('/api/gaps')).then(r=>r.json()).then(g=>{
    $('gapn').textContent = g.length ? `— 全体で ${g.length} 件` : '';});
  const w=WL.find(x=>x.post_id==pid);
  $('warn').innerHTML = w&&w.written>0
    ? `<span class="hint">この post には既に所見 ${w.written} 件が確定済みです。<b>「追加分を確定」は既に入っている内容を飛ばす</b>ので、続きから書いて何度でも押せます。</span>`
    : '';
  pruneEmpty(ann);   // 旧い下書きに残っている空の器を開いた時点で片付ける
  // 計時は scene ごと。下書きに積算済みの秒があれば引き継ぐ (リロードで消さない)。
  activeMs=(ann.seconds_active||0)*1000; activeAt=performance.now(); minutesTouched=false;
  if(ann.minutes!=null){ $('minutes').value=ann.minutes; minutesTouched=true }
  $('msg').textContent='未検証'; DERIVED=null; drawNav(); render(); loadObsReview();
  checkReady();          // 開いた時点で「確定できるか」を出す (押すまで判らないのを防ぐ)
  // 色モードの測定は 1 枚 2.6 秒なので一覧では取らない。開いたこの 1 枚だけ測る。
  fetch(U('/api/derived/')+pid).then(r=>r.json()).then(d=>{
    if(cur===pid){ DERIVED=d; render() }});
}
// 検出器の提案を**足す**。既存の人物は消さない (人の入力が機械に負けないように)。
// 実測 (2026-07-28、注釈済み 8 件): 顔検出の人数一致は 3/8。**一致した 3 件はすべて
// 1 人の画像**で、複数人の画像は全て過小に出た — この題材では追加の人物の頭が
// 画面外に切れていることが多く、顔でも person でも COCO 汎用でも拾えない。
// よって「出発点」以上には使えない。自動では絶対に走らせない。
async function proposeSubjects(){
  const btn=$('propbtn'); btn.disabled=true; $('propmsg').textContent='検出中…';
  try{
    const r=await (await fetch(U('/api/propose/')+cur)).json();
    if(r.error){ $('propmsg').innerHTML='<span class="e">'+r.error+'</span>'; return }
    const near=(a,b)=>Math.hypot(a.x-b.x,a.y-b.y)<0.06;
    let added=0;
    (r.subjects||[]).forEach(ps=>{
      if(ann.subjects.some(s=>s.anchor&&near(s.anchor,ps.anchor))) return;  // 既にある
      const s={ref:'person_'+(ann.subjects.length+1),presentation:'',bbox:ps.bbox||null,
        anchor:ps.anchor,regions:[],attributes:[],poses:[],identity:{species:'human'},objects:[]};
      ann.subjects.push(s); added++;
    });
    // 中身が空のままの初期人物が残っていたら、提案で置き換わったぶんを片付ける
    if(added) ann.subjects=ann.subjects.filter((s,i)=>
      s.anchor||s.bbox||s.regions.length||s.attributes.length||s.poses.length||s.presentation);
    $('propmsg').innerHTML=`顔 ${(r.faces||[]).length} / 人物 ${(r.persons||[]).length} を検出 → `
      + `<b>${added} 人を追加</b>。<b>頭が画面外の人物は出ません</b>`
      + `<span class="hint"> (実測の人数一致 3/8 — 足りない分は手で足し、余計なものは削除してください)</span>`;
    render(); touch();
  }catch(e){ $('propmsg').innerHTML='<span class="e">'+e+'</span>' }
  finally{ btn.disabled=false }
}
function addSubj(silent){ann.subjects.push({ref:'person_'+(ann.subjects.length+1),kind:'person',presentation:'',
  bbox:null,regions:[],attributes:[],poses:[],identity:{species:'human'},objects:[]});
  act=ann.subjects.length-1; if(!silent)render()}
function addRel(){ann.relations.push({relation:'',subject:{ref:'',region:''},object:{ref:'',region:''}});render()}
function addCount(){ann.entity_counts.push({presentation:'',count_min:1,count_exact:true});render()}
function addUnres(){ann.unresolved.push({domain:'attribute',attribute:'',value:'',note:''});render()}

// **カテゴリごとにドロップダウンを分割する** (2026-07-28 指摘)。
// 1 つの巨大リストから探すより、カテゴリの箱がそれぞれ「＋」を持つ方が速く、
// 「左腕は上げて右腕は背中」のように **同じカテゴリに複数足す**のも自然になる。
// 部位ごとに、その部位に付く属性を**直下**に出す。
// 「breast を選んだのに breast_size が遠くにある」を解消する。
// pose も同じ箱に入れる。pose は特定の部位ではなく scope (動く体の部分) に付くので、
// **部位カテゴリ**に従属させる (arm_up を hand チップの下に置くのは捏造になる)。
function poseLines(s,g){
  const mine=V.pose.filter(p=>p.rg===g), out=[];
  if(!mine.length) return out;
  s.poses.forEach((x,i)=>{
    const slug=(typeof x==='string')?x:x.pose;
    const meta=mine.find(p=>p.slug===slug); if(!meta) return;
    const line=el('div',{cls:'rg p-line'});
    line.append(el('span',{cls:'a-n'},slug),
      ...hmMarks('pose',slug,
        (((HUMANOBS[s.ref]||{}).poses)||new Map()).has(slug),
        (((MODELOBS[s.ref]||{}).poses)||new Map()).has(slug)));
    // 左右を書けるのは対の肢だけ。「左腕は上げて右腕は背中」は同じ箱に 2 つ足す。
    if(meta.lat){
      const lt=opt(['na','left','right'],(typeof x==='object'&&x.laterality)||'','左右');
      lt.classList.add('lat');
      lt.addEventListener('change',()=>{
        if(typeof s.poses[i]==='string')s.poses[i]={pose:s.poses[i]};
        s.poses[i].laterality=lt.value||undefined; render()});
      line.append(lt);
    }
    line.append(el('button',{cls:'del',on:{click:()=>{s.poses.splice(i,1);render()}}},'×'));
    out.push(line);
  });
  const cur=s.poses.map(x=>(typeof x==='string')?x:x.pose);
  // 同じ pose を左右で 2 つ足せるようにするため、bilateral な scope は選択済みでも残す
  const avail=mine.filter(p=>p.lat||!cur.includes(p.slug));
  // **ポーズの箱から関係の形にも辿れるようにする** (user 指摘 2026-07-31
  // 「どこのポーズにも見当たらない」)。抱える・羽交い締めのような**接触を含む形**は
  // 設計上 pose ではなく関係で書くが、注釈者はポーズの箱を探す。
  // ここに出さないと「無い」と読まれる — **保存の形と探す場所は別**。
  // 選ぶと型が当たり、関係として入る (pose は増えない)。
  const macroItems=(V.macros||[]).map(m=>({
    slug:'macro:'+m.slug, n:m.phrase,
    d:mdPlain(m.d).slice(0,90), g:'※ ポーズではなく関係で書く形 (選ぶと関係が入ります)'}));
  const items=avail.map(p=>({slug:p.slug, n:p.slug, d:p.d, g:p.g||''}))
                   .concat(macroItems);
  if(items.length){
    const add=opt(items,'','＋ポーズ / 形');
    add.addEventListener('change',()=>{
      if(!add.value) return;
      if(add.value.startsWith('macro:')){
        const m=(V.macros||[]).find(x=>'macro:'+x.slug===add.value);
        if(m){ const other=(ann.subjects.find(y=>y.ref&&y.ref!==s.ref)||{}).ref;
               $('say').value=s.ref+' '+m.phrase+(other?' '+other:'');
               applySay(); }
        return;
      }
      s.poses.push({pose:add.value}); render();
    });
    out.push(el('div',{cls:'rg p-line'},withdesc(add)));
  }
  return out;
}
// 部位に紐づかない属性でも、表示先カテゴリが決まっているものはその箱に出す
// (妊娠 → 胴体)。observation 側は region を持たないままなので、意味は変わらない。
function entityAttrLines(s,g){
  const mine=Object.keys(V.entity_attr_group||{}).filter(a=>V.entity_attr_group[a]===g);
  if(!mine.length) return [];
  const out=[];
  s.attributes.filter(a=>!a.region&&mine.includes(a.attribute)).forEach(a=>{
    const meta=V.attribute.find(x=>x.slug===a.attribute);
    const valw=opt(meta?meta.values:[],a.value||'','値');
    valw.addEventListener('change',()=>{a.value=valw.value});
    out.push(el('div',{cls:'rg e-line'},el('span',{cls:'a-n'},a.attribute),
      ...attrMarks(s.ref,a),withdesc(valw),
      ...textField(a),
      el('button',{cls:'del',on:{click:()=>{
        s.attributes.splice(s.attributes.indexOf(a),1); render()}}},'×')));
  });
  const avail=mine.filter(x=>!s.attributes.some(a=>!a.region&&a.attribute===x));
  const items=attrValueItems(avail);
  if(items.length){
    const add=opt(items,'','＋人物全体の属性・値');
    add.addEventListener('change',()=>{if(add.value){pushAttr(s,add.value); render()}});
    out.push(el('div',{cls:'rg e-line'},withdesc(add)));
  }
  return out;
}
// 属性の選択と値の選択を **1 回**で済ませる (2026-07-28 指摘)。
// 見出し = 属性 (と説明)、項目 = その属性の値。選ぶと {attribute, value} が同時に決まる。
// 値を持たない属性 (continuous / 値未定義) は属性そのものを 1 項目として出す。
const SEP='\u241F';
function attrValueItems(slugs){
  const out=[];
  slugs.forEach(x=>{
    const m=V.attribute.find(y=>y.slug===x); if(!m) return;
    const head=m.d?`${x} — ${m.d}`:x;
    if(m.value_type==='continuous'||!m.values.length){
      out.push({slug:x, n:x, d:'(値は数値で後から入れる)', g:head});
    }else{
      m.values.forEach(v=>out.push({slug:x+SEP+v.slug, n:v.slug, d:v.d, g:head}));
    }
  });
  return out;
}
// 選択結果を s.attributes の 1 エントリに変える
function pushAttr(s,key,region){
  const i=key.indexOf(SEP);
  const at=i<0?key:key.slice(0,i), val=i<0?'':key.slice(i+1);
  const e={attribute:at, value:val};
  if(region) e.region=region;
  s.attributes.push(e);
}
// 多値の属性 (attribute.is_multivalued) はチェックボックスで出す。
// 「汗と精液が同時に付いている」を 1 行で書けるようにするため (2026-07-28 指摘)。
// 観測側は値ごとに別 observation なので、s.attributes には値の数だけエントリを持つ。
function multiAttrBlock(s,rg,meta,bilateral){
  const mine=()=>s.attributes.filter(a=>a.region===rg&&a.attribute===meta.slug);
  const wrap=el('div',{cls:'rg a-line multi'});
  wrap.append(el('span',{cls:'a-n'},meta.slug));
  const boxes=el('div',{cls:'ckset'});
  meta.values.forEach(v=>{
    const hit=mine().find(a=>a.value===v.slug);
    const cb=el('input',{type:'checkbox'});
    if(hit) cb.checked=true;
    cb.addEventListener('change',()=>{
      if(cb.checked){
        // 値が空のエントリ (＋属性 で作った器) があれば再利用する
        const empty=mine().find(a=>!a.value);
        if(empty) empty.value=v.slug;
        else s.attributes.push({attribute:meta.slug,value:v.slug,region:rg});
      }else{
        const idx=s.attributes.findIndex(a=>a.region===rg&&a.attribute===meta.slug&&a.value===v.slug);
        if(idx>=0) s.attributes.splice(idx,1);
        // 最後の 1 つを外してもブロックは残す (器を 1 つ空で残す)
        if(!mine().length) s.attributes.push({attribute:meta.slug,value:'',region:rg});
      }
      render();
    });
    boxes.append(el('label',{cls:'ck'+(hit?' on':''),title:tipShort(v.d)||v.slug},cb,v.slug,
      ...(hit?attrMarks(s.ref,{attribute:meta.slug,value:v.slug,region:rg}):[])));
  });
  wrap.append(boxes);
  if(bilateral){
    // 左右は**ブロック単位**。左右で付着物が違うなら、部位の左右を分けて観測する
    const cur=mine().find(a=>a.laterality)||{};
    const lat=opt(['na','left','right'],cur.laterality||'','左右');
    lat.classList.add('lat');
    lat.addEventListener('change',()=>{
      mine().forEach(a=>a.laterality=lat.value||undefined)});
    wrap.append(lat);
  }
  wrap.append(el('button',{cls:'del',title:'この属性ごと消す',on:{click:()=>{
    s.attributes=s.attributes.filter(a=>!(a.region===rg&&a.attribute===meta.slug)); render()}}},'×'));
  return wrap;
}
// **1 人の部位カテゴリは分断しない。** 一度は左右に割ったが、同じ人物の頭と股が
// 画面の別の端に出て辿れなくなった (2026-07-28 指摘) ので人物は右へ一本化した。
// 左右の分割はセクション単位 (人物 / それ以外) で行う。
function regionGroups(s,kind){
  const L=norm(V.region);
  const box=el('div');
  const order=[], byg=new Map();
  L.forEach(v=>{const g=v.g||''; if(!byg.has(g)){byg.set(g,[]);order.push(g)} byg.get(g).push(v)});
  // creature は**非人間の部位を先頭へ**。人体のカテゴリも残す — 犬にも口や舌はあり、
  // 「口が外陰部に触れている」は creature 側の口で書く必要がある (639940)。
  // 消すのではなく順番を変えるだけ。
  if(kind==='creature'){
    const k='非人間の部位', j=order.indexOf(k);
    if(j>0){ order.splice(j,1); order.unshift(k) }
  }
  order.forEach(g=>{
    const mine=s.regions.filter(r=>byg.get(g).some(v=>v.slug===r));
    const row=el('div',{cls:'row grp'});
    row.append(el('span',{cls:'grp-l'},g||'その他'));
    const cell=el('div',{cls:'grp-cell'});
    mine.forEach(rg=>{
      const line=el('div',{cls:'rg'});
      line.append(el('span',{cls:'tag'},rg,
        ...hmMarks('region',rg,
          (((HUMANOBS[s.ref]||{}).regions)||new Map()).has(rg),
          (((MODELOBS[s.ref]||{}).regions)||new Map()).has(rg)),
        el('i',{title:'削除',on:{click:()=>{
          s.regions.splice(s.regions.indexOf(rg),1);
          s.attributes=s.attributes.filter(a=>a.region!==rg); render()}}},'×')));
      // この部位に付く属性のうち、まだ付けていないものを足せる combobox
      const avail=(V.region_attrs[rg]||[]).filter(x=>!s.attributes.some(a=>a.attribute===x&&a.region===rg));
      const items=attrValueItems(avail);
      if(items.length){
        const add=opt(items,'','＋属性・値');
        add.addEventListener('change',()=>{if(add.value){
          pushAttr(s,add.value,rg); render()}});
        line.append(add);
      }
      // 関係の**主語側 (誰の・どこ) はここで決まっている**ので選ばせない (2026-07-28 指摘)。
      // 部位から関係を作れば、あとは「何を」「誰の何に」だけが残る。
      const rel=opt(V.relation,'','＋関係');
      rel.addEventListener('change',()=>{if(rel.value){
        ann.relations.push({relation:rel.value,
          subject:{ref:s.ref,region:rg}, object:{ref:'',region:''}});
        render()}});
      line.append(rel);
      cell.append(line);
      // 既に付けた属性を部位の直下に並べる。多値の属性 (surface_substance) は
      // **同じ属性の複数エントリを 1 ブロックにまとめて**チェックボックスで出す。
      const shown=new Set();
      s.attributes.filter(a=>a.region===rg).forEach(a=>{
        const meta=V.attribute.find(x=>x.slug===a.attribute);
        if(meta&&meta.multi){
          if(shown.has(a.attribute)) return;      // ブロックは属性ごとに 1 つ
          shown.add(a.attribute);
          cell.append(multiAttrBlock(s,rg,meta,(byg.get(g).find(v=>v.slug===rg)||{}).lat));
          return;
        }
        const nm=el('span',{cls:'a-n'},a.attribute);
        let valw;
        if(meta&&meta.value_type==='continuous'){
          valw=el('input',{type:'number',step:'0.05',value:a.numeric??'',placeholder:'0..1',style:'width:82px'});
          valw.addEventListener('input',()=>{a.numeric=valw.value===''?null:parseFloat(valw.value)});
        }else{
          valw=opt(meta?meta.values:[],a.value||'','値');
          valw.addEventListener('change',()=>{a.value=valw.value});
        }
        const line=el('div',{cls:'rg a-line'},nm,...attrMarks(s.ref,a),
                     withdesc(valw),...textField(a));
        // 「左右」は **bilateral な部位のときだけ**。navel や mons に出しても選べる値が
        // 無いのに 1 行使ってしまう (2026-07-28 指摘)。
        if((byg.get(g).find(v=>v.slug===rg)||{}).lat){
          const lat=opt(['na','left','right'],a.laterality||'','左右');
          lat.classList.add('lat');
          lat.addEventListener('change',()=>{a.laterality=lat.value||undefined});
          line.append(lat);
        }
        line.append(el('button',{cls:'del',on:{click:()=>{
          s.attributes.splice(s.attributes.indexOf(a),1); render()}}},'×'));
        cell.append(line);
      });
    });
    const add=opt(byg.get(g).filter(v=>!s.regions.includes(v.slug)),'','＋部位');
    add.addEventListener('change',()=>{if(add.value){s.regions.push(add.value);render()}});
    cell.append(el('div',{cls:'rg'},withdesc(add)));
    poseLines(s,g).forEach(x=>cell.append(x));
    entityAttrLines(s,g).forEach(x=>cell.append(x));
    // 突合: このカテゴリの部位・ポーズ・属性への人間/モデルの確定所見を
    // コントロールの直下に出す (右パネルまで視線を往復させない)
    const gRegs=new Set(byg.get(g).map(v=>v.slug));
    const rrow=hmSetRow(s.ref,'region',gRegs,new Set(s.regions||[]),
      v=>{s.regions=s.regions||[];if(!s.regions.includes(v))s.regions.push(v);render()});
    if(rrow) cell.append(rrow);
    const gPoses=new Set(V.pose.filter(p=>p.rg===g).map(p=>p.slug));
    const prow=hmSetRow(s.ref,'pose',gPoses,
      new Set((s.poses||[]).map(x=>typeof x==='string'?x:x.pose)),
      v=>{s.poses=s.poses||[];
          if(!s.poses.some(x=>(typeof x==='string'?x:x.pose)===v))s.poses.push({pose:v});
          render()});
    if(prow) cell.append(prow);
    const arow=hmAttrRow(s,gRegs);
    if(arow) cell.append(arow);
    row.append(cell); box.append(row);
  });
  return box;
}
function chips(arr,list,ph,onchange){
  const box=el('div',{cls:'row'});
  arr.forEach((v,i)=>box.append(el('span',{cls:'tag'},v,
    el('i',{title:'削除',on:{click:()=>{arr.splice(i,1);onchange()}}},'×'))));
  const s=opt(norm(list).filter(x=>!arr.includes(x.slug)),'',ph);
  s.addEventListener('change',()=>{if(s.value){arr.push(s.value);onchange()}});
  box.append(withdesc(s)); return box;
}
// カテゴリ (g) ごとに 1 行。部位・ポーズと同じ形にして、カメラ・構図も
// 平坦な 47 語から探さなくて済むようにする (2026-07-28 指摘)。
function chipsGrouped(arr,list,onchange){
  const L=norm(list), box=el('div');
  const order=[], byg=new Map();
  L.forEach(v=>{const g=v.g||''; if(!byg.has(g)){byg.set(g,[]);order.push(g)} byg.get(g).push(v)});
  order.forEach(g=>{
    const row=el('div',{cls:'row grp'});
    row.append(el('span',{cls:'grp-l'},g||'その他'));
    const cell=el('div',{cls:'grp-cell'});
    const line=el('div',{cls:'rg'});
    arr.filter(v=>byg.get(g).some(x=>x.slug===v)).forEach(v=>{
      line.append(el('span',{cls:'tag'},v,
        el('i',{title:'削除',on:{click:()=>{arr.splice(arr.indexOf(v),1);onchange()}}},'×')));
    });
    const add=opt(byg.get(g).filter(x=>!arr.includes(x.slug)),'','＋'+(g||'追加'));
    add.addEventListener('change',()=>{if(add.value){arr.push(add.value);onchange()}});
    line.append(add); cell.append(line); row.append(cell); box.append(row);
  });
  return box;
}
// 0045: **身につけているものの入力。** それまで vocab に V.object を送っていたのに
// **画面に欄が無く**、object の所見は 1 件も書けなかった (実測 0 件)。
// ピアスは部位が本体なので「どこに」を必ず出す — nipple / navel / ear / tongue /
// clitoris を区別できなければ書く意味が無い。部位は任意 (服は言わなくてよい)。
function objRows(s){ return objBox(s, false) }
// 0062: **ピアスを身につけているものから独立させる** (user 指摘 2026-07-31)。
// 「今の管理は判りにくい」 — 服と同じ行に混ざると、部位・形・左右・数という
// **ピアス固有の 4 欄**が服の行にも出て、どちらの話か判らなくなっていた。
function pierceRows(s){ return objBox(s, true) }
function objBox(s, pierce){
  const box=el('div');
  (s.objects||[]).filter(o=>(o.object==='piercing')===pierce).forEach(o=>{
    const i=s.objects.indexOf(o);
    const row=el('div',{cls:'row'});
    // ピアスの箱では種類を選ばせない (piercing 固定)。**部位が本体**なので先に出す
    const ob=pierce?null:opt(V.object, o.object||'', '身につけているもの');
    if(ob) ob.addEventListener('change',()=>{o.object=ob.value; render()});
    const st=opt(V.object_state, o.state||'', '状態');
    st.addEventListener('change',()=>{o.state=st.value});
    // 部位は「見えている部位」に挙げたものを先に出す — ピアスは大抵その部位も見えている
    const mine=(s.regions||[]).map(r=>({slug:r}));
    const rest=V.region.filter(r=>!(s.regions||[]).includes(r.slug));
    const rg=opt(mine.concat(rest), o.region||'', pierce?'どこに':'どこに (任意)');
    rg.addEventListener('change',()=>{o.region=rg.value});
    // 0046: 形と数。ピアスは「耳のリング」と「耳のバー」が別の事実なので形も出す
    const vr=opt(V.object_variant||[], o.variant||'', pierce?'形':'形 (任意)');
    vr.addEventListener('change',()=>{o.variant=vr.value});
    // 0047: **対になっている部位のときだけ**左右を出す。midline の部位に出すと
    // 意味の無い所見が書けてしまう (0010 の laterality の意図と同じ)。
    // 「両耳に 1 個ずつ」は left の行と right の行の 2 行で書く。
    const rmeta=(V.region||[]).find(x=>x.slug===o.region);
    let lt=null;
    if(rmeta && rmeta.lat === true){          // region_rows が bilateral を真偽値で送る
      lt=opt([{slug:'left',n:'左'},{slug:'right',n:'右'},
              {slug:'__both',n:'左右そろって',d:'選ぶと左と右の 2 行に分かれます'},
              {slug:'na',n:'左右の別なし',d:'「両方」ではなく「左右を区別しない」'}],
             o.laterality||'', '左右 (任意)');
      lt.addEventListener('change',()=>{
        if(lt.value==='__both'){
          // **保存されるのは left と right の 2 所見。** `both` という値は作らない —
          // 作ると「両方」と「片側に 2 個」が同じ値空間に混ざり、0047 で左右を
          // 別の事実として分けた意味が消える。ここは**入力の手間だけ**を減らす。
          o.laterality='left';
          s.objects.splice(i+1, 0, Object.assign({}, o, {laterality:'right'}));
        } else o.laterality=lt.value;
        render();
      });
    }
    const cn=el('input',{type:'number',min:'1',step:'1',value:o.count??'',
                         placeholder:'数',style:'width:70px',title:'その部位にいくつ (任意)'});
    cn.addEventListener('input',()=>{o.count = cn.value===''? null : parseInt(cn.value,10)});
    row.append(...(ob?[ob]:[]), ...(pierce?[rg,vr,st]:[st,rg,vr]),
               ...(lt?[lt]:[]), cn, el('button',{cls:'del',on:{click:e=>{
      e.stopPropagation(); s.objects.splice(i,1); render()}}},'×'));
    box.append(row);
  });
  if(pierce){
    box.append(el('button',{cls:'sub',on:{click:()=>{
      s.objects=s.objects||[]; s.objects.push({object:'piercing'}); render()}}},'＋ピアス'));
  }else{
    // V.object は**文字列の配列** — x.slug で弾こうとした版は素通りした (2026-07-31)
    const add=opt(V.object.filter(x=>(x.slug||x)!=='piercing'),'','＋身につけているもの');
    add.addEventListener('change',()=>{ if(add.value){
      s.objects=s.objects||[]; s.objects.push({object:add.value}); render() }});
    box.append(add);
  }
  return box;
}
// 0070: **この ontology で唯一の自由入力**。書いてある文字は語彙にできないので置く。
// **メモ欄ではない** — 許すのは V.text_attrs の属性だけで、DB の CHECK と同じ線。
function textField(a){
  if(!(V.text_attrs||[]).includes(a.attribute)) return [];
  const inp=el('input',{value:a.text||'', maxlength:'200',
    placeholder:'書いてある文字 (任意)', style:'flex:1 1 120px;min-width:90px',
    title:'見えている文字をそのまま。翻訳も解釈もしない。空欄は「読んでいない / 読めない」'});
  inp.addEventListener('input',()=>{a.text=inp.value||undefined});
  return [inp];
}
function attrRows(s,entityOnly){
  const box=el('div');
  const placed=V.entity_attr_group||{};
  s.attributes.filter(a=>!a.region&&!placed[a.attribute]).forEach((a)=>{
    const i=s.attributes.indexOf(a);
    const meta=V.attribute.find(x=>x.slug===a.attribute);
    // 現在の値は**必ず候補に入れる**。部位に付く属性 (facial_expression 等) を、その部位を
    // まだ挙げていないせいでここに残しているとき、候補に無いと選択が空欄に見えてしまう。
    const names=entityOnly
      ? V.entity_attrs.filter(x=>!placed[x])
         .concat(a.attribute&&!V.entity_attrs.includes(a.attribute)?[a.attribute]:[])
      : V.attribute.map(x=>x.slug);
    const pool=names.map(x=>{const m=V.attribute.find(y=>y.slug===x);return {slug:x,d:m?m.d:''}});
    const sel=opt(pool,a.attribute,'attribute');
    // 部位に付く属性がここに居る = その部位をまだ「見えている部位」に挙げていない
    const rmeta=meta&&meta.applies_kind==='region'?meta.applies_key:null;
    sel.addEventListener('change',()=>{a.attribute=sel.value;delete a.value;delete a.numeric;render()});
    let valw;
    if(meta&&meta.value_type==='continuous'){
      valw=el('input',{type:'number',step:'0.05',value:a.numeric??'',placeholder:'0..1',style:'width:88px'});
      valw.addEventListener('input',()=>{a.numeric=valw.value===''?null:parseFloat(valw.value)});
    }else{
      valw=opt(meta?meta.values:[],a.value||'','値');
      valw.addEventListener('change',()=>{a.value=valw.value});
    }
    const row=el('div',{cls:'row'},withdesc(sel),withdesc(valw),
      ...attrMarks(s.ref,a), ...textField(a),
      el('button',{cls:'del',on:{click:()=>{s.attributes.splice(i,1);render()}}},'×'));
    if(rmeta){
      // ワンクリックで正しい場所 (部位の下) へ移せるようにする — 手で消して足し直させない
      row.append(el('button',{cls:'sub',title:rmeta+' を見えている部位に足して、その下へ移します',
        on:{click:()=>{if(!s.regions.includes(rmeta))s.regions.push(rmeta);
          a.region=rmeta; render()}}},'→ '+rmeta+' の下へ'));
    }
    box.append(row);
  });
  box.append(el('button',{cls:'sub',on:{click:()=>{s.attributes.push({attribute:'',value:''});render()}}},'＋属性'));
  return box;
}
// 位置は **1 人なら不要**、複数人なら点が主。
// 実測: kura の post 756,534 のうち solo は 162,218 (21.4%) — 79% が 2 人以上で、
// 絡み合った 2 人の box はほぼ同一になり主体を同定できない。頭の中心なら別の位置になる。
// 旧い下書きは kind='head_center' を持つ。捨てずに読む (0028 の移行)。
const anchorRegionOf=a=>a.region||((a.kind||'head_center').replace(/_center$/,''));
function posLabel(s){
  if(ann.subjects.length<2) return '1 人なので位置は不要';
  const bits=[];
  if(s.anchor) bits.push(`${anchorRegionOf(s.anchor)} (${s.anchor.x.toFixed(2)}, ${s.anchor.y.toFixed(2)})`);
  if(s.bbox) bits.push('bbox あり');
  return bits.length?bits.join(' / '):'位置 未設定';
}
// 0040: **species は creature にも出す。** person のときだけ出していた版では
// 「犬」「馬」を主体として名指せなかった (user 指摘 2026-07-30)。
// motif は「何に似せてあるか」なので person だけ — 猫耳の人間は
// species=human + motif=cat。catgirl のような合成語は非推奨で validator が弾く。
// listener は index ではなく**生成した要素に直接**付ける。
// querySelectorAll('.cb')[n] は select が 1 つ増えるだけで壊れる。
function idFields(s,kind){
  if(kind==='object') return [];
  s.identity=s.identity||{};
  const out=[];
  const sp=opt(V.species,s.identity.species||'','species (何であるか)');
  sp.addEventListener('change',e=>{s.identity.species=e.target.value});
  out.push(el('label',{},kind==='creature'?'species — 犬・馬など「何であるか」':'species'), sp,
    ...hmValChips(s.ref,'species',()=>(s.identity||{}).species||'',
                  v=>{s.identity=s.identity||{};s.identity.species=v}));
  if(kind==='person'){
    const mo=opt(V.species,s.identity.motif_species||'','motif (何に似せてあるか)');
    mo.addEventListener('change',e=>{s.identity.motif_species=e.target.value});
    out.push(el('label',{},'動物モチーフ — 猫耳なら cat (species は human のまま)'), mo);
  }
  return out;
}
function subjCard(s,i){
  const kind=s.kind||'person';
  const kd=opt(V.entity_kind,kind,'kind');
  kd.addEventListener('change',()=>{s.kind=kd.value||'person';
    // 人でなくなったら人前提の欄を落とす (残すと「人ではないのに presentation がある」
    // という矛盾した記録になる)
    if(s.kind!=='person'){delete s.presentation; s.identity={};}
    render()});
  const head = el('header',{cls:'row'},
    el('span',{cls:'who',title:'画像上の印と同じ色'},el('b',{}),String(i+1)),
    el('input',{value:s.ref,style:'width:110px',title:'ref'}),
    kd,
    ...(kind==='person'?[opt(V.presentation,s.presentation,'presentation'),
      ...hmValChips(s.ref,'presentation',()=>s.presentation||'',
                    v=>{s.presentation=v})]:[]),
    el('span',{cls:'hint'},posLabel(s)),
    el('button',{cls:'del',on:{click:e=>{e.stopPropagation();
      // **この人物を指している関係の参加者も外す。** 残すと『subjects に無い ref』として
      // 確定時に弾かれ、原因が離れた場所にあって追いにくい (2026-07-28 実測)。
      const gone=ann.subjects[i].ref;
      ann.relations.forEach(r=>['subject','object'].forEach(k=>{
        if(r[k]&&r[k].ref===gone){r[k].ref='';r[k].region=''}}));
      ann.subjects.splice(i,1);act=0;render()}}},'削除'));
  // **identity はカードの先頭に置く。** 末尾に置いていた版では、人間用の部位カテゴリ
  // 16 個 (それぞれ空でも「＋部位」を出す) がカード高を ~1050px に押し上げ、
  // **creature の species が画面外に落ちて見えなかった** (user 指摘 2026-07-30)。
  // 「何であるか」は最初に決める事実で、creature では他の選択の前提にもなる。
  // 突合はここでまとめて出さない — 各コントロールの真横のチップに統合した
  // (user 指摘 2026-07-31「コントロールと突合が離れすぎ」)。
  const card=el('div',{cls:'subj'+(i===act?' act':''),style:`--c:${subjColor(i)}`}, head,
    ...idFields(s,kind),
    el('label',{},'見えている部位 — 属性とポーズはその部位カテゴリの下に出ます'),
    regionGroups(s,kind),
    el('label',{},'全体の属性 (部位に紐づかないもの)'), attrRows(s,true),
    hmAttrRow(s,''),
    el('label',{},'ピアス — どこに / 形 / 左右 / 数。「左右そろって」は 2 行に分かれます'),
    pierceRows(s),
    el('label',{},'身につけているもの'), objRows(s));
  // 選択切替はカードの「地」をクリックしたときだけ。
  // これが無いと <select> を開いたクリックが親へ bubble して render() が走り、
  // 開いた瞬間に select が DOM ごと作り直されて**選べなくなる** (2026-07-27 実害)。
  card.addEventListener('click',e=>{
    if(e.target.closest('select,input,textarea,button,option')) return;
    if(act===i) return;                 // 同じカードの再クリックで無駄に作り直さない
    act=i; render();
  });
  const refIn=card.querySelector('input'); refIn.addEventListener('input',()=>{s.ref=refIn.value});
  if(kind==='person'){
    const sels=card.querySelectorAll('.cb');
    sels[1].addEventListener('change',e=>{s.presentation=e.target.value});  // kind の次
  }
  return card;
}
// 関係 1 件の行。**噴出物の箱と共用** (0062) — only を渡すと関係の候補をそれだけに絞る。
function relRow(r,i,refs,refColor,only){
    const rl=opt(only?V.relation.filter(x=>only.includes(x.slug)):V.relation,
                 r.relation, only?'何が':'関係');
    const or=opt(refs,r.object.ref,'誰の'), og=opt(V.region,r.object.region,'どこ(任意)');
    // 0052: 対象は**相手の部位 か 環境 (地面・空中)** のどちらか。両方は書けない
    // (DB 側も CHECK で弾く)。片方を選んだらもう片方を消す。
    const sf=opt(V.surface||[], r.object.surface||'', 'または環境');
    rl.onchange=()=>r.relation=rl.value;
    or.onchange=()=>{r.object.ref=or.value; if(or.value) delete r.object.surface; render()};
    og.onchange=()=>{r.object.region=og.value; if(og.value) delete r.object.surface; render()};
    sf.onchange=()=>{r.object.surface=sf.value||undefined;
                     if(sf.value){r.object.ref='';r.object.region=''} render()};
    // **bilateral な部位のときだけ左右を出す。** これが無いと「右手で A を、左手で B を」
    // が書けず、同じ hand を主語にした 2 行が区別できない (2026-07-29 指摘)。
    const latSel=(side)=>{
      const m=V.region.find(x=>x.slug===side.region);
      if(!m||!m.lat) return null;
      const s=opt(['na','left','right'],side.laterality||'','左右');
      s.classList.add('lat');
      s.addEventListener('change',()=>{side.laterality=s.value||undefined});
      return s;
    };
    // **関係 1 件を 1 つのブロックにする。** 折り返して 3 行になるので、区切りが無いと
    // どこからどこまでが 1 つの関係か判らない。左端の帯を**主語の人物の色**にして
    // 「誰の関係か」を一目で分けられるようにする (2026-07-29 指摘)。
    const sc=refColor(r.subject.ref);
    const row=el('div',{cls:'row rel',style:sc?`--c:${sc}`:''});
    // 部位から作った関係は主語が決まっている → チップで見せるだけ。
    // × を押すと解除され、従来どおり選択欄に戻る (取り違えたときの逃げ道)。
    if(r.subject.ref&&r.subject.region){
      const c=refColor(r.subject.ref);
      row.append(el('span',{cls:'tag subj-fix',style:c?`--c:${c}`:''},
        c?el('em',{cls:'dot',style:`--c:${c}`}):'',
        r.subject.ref+' / '+r.subject.region,
        el('i',{title:'主語を選び直す',on:{click:()=>{r.subject.region='';render()}}},'×')));
      const sl=latSel(r.subject); if(sl) row.append(sl);
    }else{
      const sr=opt(refs,r.subject.ref,'誰の'), sg=opt(V.region,r.subject.region,'どこ(任意)');
      sr.onchange=()=>r.subject.ref=sr.value; sg.onchange=()=>r.subject.region=sg.value;
      const sl=latSel(r.subject); row.append(sr,sg); if(sl) row.append(sl);
    }
    // 環境を選んでいるときは人体側を出さない (選べてしまうと矛盾を作れる)
    row.append('→',withdesc(rl),'→', ...(r.object.surface?[sf]:[or,og,sf]));
    const ol=latSel(r.object); if(ol) row.append(ol);
    row.append(el('button',{cls:'del',on:{click:()=>{ann.relations.splice(i,1);render()}}},'×'));
    return row;
}

// 0062: 「出ているか」を人物ごとに 1 行で出す。**全体の属性の箱から移した** —
// 4 つ並んでも何の話か判らず、行き先を言う関係とも離れていた (user 指摘)。
// 0064/0072: **主体をここで選べるようにする。** 「選択中の人物」というカードの
// 選択だけに頼っていた版は、カードの地をクリックし損ねると選択が変わらず、
// **person_2 を選んだつもりで person_1 が主体になった** (user 指摘 2026-07-31)。
// 見えない全体状態に頼らず、**使う場所に置く**。ここで変えるとカードの選択も動く。
function sayWho(){
  const box=$('saywho'); if(!box) return;
  const refs=ann.subjects.map((s,i)=>({slug:s.ref||('#'+(i+1)), c:subjColor(i)}))
                         .filter(x=>x.slug);
  if(!refs.length){ box.replaceChildren(el('span',{cls:'hint'},'人物がいません')); return }
  const cur=(ann.subjects[act]||ann.subjects[0]||{}).ref||'';
  const sel=opt(refs, cur, '誰が');
  sel.addEventListener('change',()=>{
    const i=ann.subjects.findIndex(x=>x.ref===sel.value);
    if(i>=0){ act=i; render() }
  });
  box.replaceChildren(sel);
}
function emisAttrs(){
  const box=el('div'), names=V.emission_attrs||[];
  ann.subjects.forEach((s,si)=>{
    (s.attributes||[]).filter(a=>!a.region&&names.includes(a.attribute)).forEach(a=>{
      const meta=V.attribute.find(x=>x.slug===a.attribute);
      const valw=opt(meta?meta.values:[],a.value||'','値');
      valw.addEventListener('change',()=>{a.value=valw.value});
      box.append(el('div',{cls:'row rel',style:`--c:${subjColor(si)}`},
        el('span',{cls:'tag subj-fix',style:`--c:${subjColor(si)}`},
           el('em',{cls:'dot',style:`--c:${subjColor(si)}`}), s.ref),
        el('span',{cls:'a-n'},a.attribute), withdesc(valw),
        el('button',{cls:'del',on:{click:()=>{
          s.attributes.splice(s.attributes.indexOf(a),1); render()}}},'×')));
    });
  });
  if(!ann.subjects.length) return box;
  const who=opt(ann.subjects.map((s,i)=>({slug:s.ref,c:subjColor(i)})).filter(x=>x.slug),'','誰の');
  const wt=opt(names.map(x=>{const m=V.attribute.find(y=>y.slug===x);
                             return {slug:x,d:m?m.d:''}}),'','＋出ているか');
  wt.addEventListener('change',()=>{
    const s=ann.subjects.find(x=>x.ref===who.value)||ann.subjects[act];
    if(!wt.value||!s) return;
    s.attributes=s.attributes||[];
    if(!s.attributes.some(a=>!a.region&&a.attribute===wt.value))
      s.attributes.push({attribute:wt.value});
    render();
  });
  box.append(el('div',{cls:'row'},who,withdesc(wt)));
  return box;
}
// 噴出物の関係は**主語を選ばせる** — 出どころが本題なので、部位から作らせるのを待たない
function addEmisRel(){
  ann.relations.push({relation:(V.emission_rels||['ejaculating_onto'])[0],
                      subject:{ref:(ann.subjects[act]||{}).ref||'',region:''},
                      object:{ref:'',region:''}});
  render();
}
// 0064: 文を**人が書いた展開の型**に当ててコントロールを埋める。
// **推測はしない** — 型は DB の phrase_macro にあり、当たらなかった語は捨てずに出す。
function refToken(tok){
  const m=/^([a-z]+)[_-]?(\d+)$/i.exec(tok); if(!m) return null;
  const k=m[1].toLowerCase();
  if(k==='p'||k==='person') return 'person_'+m[2];
  if(k==='c'||k==='creature') return 'creature_'+m[2];
  if(k==='o'||k==='object') return 'object_'+m[2];
  return null;
}
function ensureSubject(ref){
  let s=ann.subjects.find(x=>x.ref===ref);
  if(s) return {s, made:false};
  const kind=ref.startsWith('creature_')?'creature':ref.startsWith('object_')?'object':'person';
  s={ref, kind, identity:{}, regions:[], attributes:[], poses:[], objects:[]};
  ann.subjects.push(s); return {s, made:true};
}
function applySay(){
  const raw=($('say').value||'').trim();
  const out=$('sayout'); out.replaceChildren();
  if(!raw) return;
  const toks=raw.split(/[\s,;、。]+/).filter(Boolean);
  const refs=[], rest=[];
  toks.forEach(tk=>{const r=refToken(tk);
    if(r) refs.push(r); else if(ann.subjects.some(s=>s.ref===tk)) refs.push(tk); else rest.push(tk)});
  const made=[];
  // **書かれていない側は「選択中の人物」から埋める** (user 指摘 2026-07-31)。
  // 書いた ref だけを見ていた版は、person_2 を選んでいても文に ref が無ければ
  // 何も束縛されず、**`$s` / `$o` という名前の人物を作っていた**。
  // 順番は 選択中 → それ以外、で、既に使った人物は飛ばす。
  const bind={};
  // 「主体」の欄が最優先。次に選択中の人物、その次に並び順
  const picked=($('saywho')&&$('saywho').querySelector('.cb-btn')
                ? $('saywho').querySelector('.cb-btn').textContent.trim() : '');
  const fallback=[picked, (ann.subjects[act]||{}).ref]
                 .concat(ann.subjects.map(x=>x.ref))
                 .filter(x=>x&&ann.subjects.some(s=>s.ref===x));
  ['$s','$o'].forEach((k,i)=>{
    const used=Object.values(bind);
    const r=refs[i] || fallback.find(x=>!used.includes(x));
    if(!r) return;
    const e=ensureSubject(r); bind[k]=r; if(e.made) made.push(r);
  });
  // **長い型から順に当てる。** "from behind" より先に "behind" が当たると
  // 型が壊れるので、語数の多い順に走査する。
  const M=(V.macros||[]).map(m=>[m.phrase].concat(m.alias||[]).map(p=>({m,p:p.toLowerCase().split(/\s+/)}))).flat()
                        .sort((a,b)=>b.p.length-a.p.length);
  const low=rest.map(x=>x.toLowerCase());
  const used=new Array(low.length).fill(false);
  const hit=[];
  M.forEach(({m,p})=>{
    for(let i=0;i+p.length<=low.length;i++){
      if(used.slice(i,i+p.length).some(Boolean)) continue;
      if(p.every((w,j)=>low[i+j]===w)){ for(let j=0;j<p.length;j++) used[i+j]=true; hit.push(m); break }
    }
  });
  const left=rest.filter((_,i)=>!used[i]);
  let nrel=0, npose=0, unbound=false;
  const sub=v=>bind[v]||v;
  hit.forEach(m=>{
    const x=m.x||{};
    (x.relations||[]).forEach(r=>{
      const R=JSON.parse(JSON.stringify(r));
      ['subject','object'].forEach(k=>{ if(R[k]&&R[k].ref){
        R[k].ref=sub(R[k].ref);
        // **`$s` のままなら人物を作らない。** 作っていた版は `$s` という名前の
        // 人物が subjects に生えた (2026-07-31 実測)。埋まらなかった側は
        // 空にして、注釈者に選ばせる
        if(String(R[k].ref).startsWith('$')){ R[k].ref=''; unbound=true; return }
        if(!ann.subjects.some(s=>s.ref===R[k].ref)){
          if(ensureSubject(R[k].ref).made) made.push(R[k].ref) }
        // **関係の部位はその人物の「見えている部位」にも入れる** — 挙げていない部位で
        // 関係は書けないので、そこを手で足させると近道にならない
        if(R[k].region){const s=ann.subjects.find(y=>y.ref===R[k].ref);
                        if(s&&!(s.regions||[]).includes(R[k].region)){s.regions=s.regions||[];
                                                                     s.regions.push(R[k].region)}}
      }});
      const same=ann.relations.some(q=>JSON.stringify(q)===JSON.stringify(R));
      if(!same){ann.relations.push(R); nrel++}
    });
    Object.entries(x.poses||{}).forEach(([who,ps])=>{
      const s=ann.subjects.find(y=>y.ref===sub(who)); if(!s) return;
      s.poses=s.poses||[];
      ps.forEach(pz=>{ if(!s.poses.some(q=>q.pose===pz)){s.poses.push({pose:pz}); npose++} });
    });
  });
  render();
  const msg=[];
  if(hit.length) msg.push(el('span',{},'当たった型: '),
    ...hit.map(m=>el('b',{cls:'k',title:mdPlain(m.d)},m.phrase+' ')));
  msg.push(el('span',{},` → 関係 ${nrel} / ポーズ ${npose}`
    + (made.length?` / 人物を作成 ${made.join(' ')}`:'')));
  if(unbound) msg.push(el('div',{cls:'bad'},
    '相手が決まらなかった箇所は空にしました — 人物を足すか、文に ref を書いてください'));
  if(left.length) msg.push(el('div',{cls:'bad'},'解釈できなかった語: '+left.join(' ')
    + ' — 型の一覧を見て言い換えてください'));
  if(!hit.length && !left.length) msg.push(el('span',{},'（型が 1 つも当たりませんでした）'));
  out.replaceChildren(...msg);
  $('say').value='';
}
function sayHelp(){
  const out=$('sayout');
  out.replaceChildren(...(V.macros||[]).map(m=>el('div',{cls:'rg'},
    el('b',{cls:'k'},m.phrase), el('span',{cls:'hint'},
      (m.alias||[]).length?' ('+m.alias.join(' / ')+') ':' '), mdFrag(m.d))));
}
// **語彙を横断して引く。** どのコントロールを開けばよいか判らないまま
// 129 部位 / 49 ポーズ / 197 属性値 を探すのは無理がある (user 指摘 2026-07-31)。
// V は既に全部 client に来ているので、**サーバを足さずに**横断できる。
function findIndex(){
  const ix=[], push=(kind,label,slug,name,d,extra)=>ix.push({kind,label,slug,name:name||slug,d:d||'',extra});
  (V.region||[]).forEach(r=>push('region','部位 / '+(r.g||''),r.slug,r.slug,r.d,r));
  (V.pose||[]).forEach(x=>push('pose','ポーズ',x.slug,x.slug,x.d,x));
  (V.relation||[]).forEach(x=>push('relation','関係',x.slug,x.slug,x.d,x));
  (V.attribute||[]).forEach(a=>{
    const where=a.applies_kind==='region'?('部位 '+a.applies_key+' に付く'):'人物全体に付く';
    push('attribute','属性 ('+where+')',a.slug,a.slug,a.d,a);
    (a.values||[]).forEach(v=>push('attr_value','属性値 — '+a.slug,a.slug+'\u241F'+v.slug,
                                   v.slug,v.d,{attr:a,val:v}));
  });
  (V.species||[]).forEach(x=>push('species','種族',x.slug,x.n||x.slug,x.d,x));
  (V.object||[]).forEach(x=>push('object','身につけているもの',x.slug||x,x.slug||x,x.d,x));
  (V.descriptor||[]).forEach(x=>push('descriptor','画角・構図 / '+(x.g||''),x.slug,x.slug,x.d,x));
  (V.surface||[]).forEach(x=>push('surface','環境 (関係の対象)',x.slug,x.n||x.slug,x.d,x));
  (V.macros||[]).forEach(m=>push('macro','型 (入力 / 検索 / 従来タグとの対応)',m.slug,m.phrase,
                                 m.d+((m.alias||[]).length?' ('+m.alias.join(' / ')+')':''),m));
  return ix;
}
let FIND_IX=null;
let MODELOBS={_rels:[],_descs:[]}, HUMANOBS={_rels:[],_descs:[]};
function runFind(){
  const q=($('find').value||'').trim().toLowerCase();
  const out=$('findout'); out.replaceChildren();
  if(!q) return;
  FIND_IX=FIND_IX||findIndex();
  // **語ごとに見る。** 文字列まるごとの一致だった版は `holdin own legs` のような
  // 打ち間違い 1 文字や語順違いで 0 件になった (user 指摘 2026-07-31)。
  // 語に分けて**全部含む**ものを出せば、途中まで打った語も当たる
  // (`holding` は `holdin` を含む)。
  const words=q.split(/[\s　]+/).filter(Boolean);
  const hay=x=>(x.slug+' '+x.name+' '+mdPlain(x.d)+' '+x.label).toLowerCase();
  const all=FIND_IX.filter(x=>{const h=hay(x); return words.every(w=>h.includes(w))});
  // 全部は当たらないときだけ、**どれかを含む**ものを出す (0 件で終わらせない)
  const loose = all.length ? [] :
    FIND_IX.filter(x=>{const h=hay(x); return words.some(w=>w.length>=2&&h.includes(w))});
  const hit=(all.length?all:loose).slice(0,60);
  if(!hit.length){
    out.append(el('div',{cls:'hint'},'該当なし — 語を短く切ってみてください '
      +'(例: 「抱え」「nelson」「泡」)'));
    return;
  }
  const s=ann.subjects[act];
  out.append(el('div',{cls:'hint'},`${hit.length} 件`
    + (all.length?'':' (全部は当たらないので、どれかを含むものを出しています)')
    + (s?` — 「入れる」は ${s.ref} に入ります`:' — 人物が居ないので入れられません')));
  hit.forEach(x=>{
    const row=el('div',{cls:'rg f-line'},
      el('b',{cls:'k'},x.name), el('span',{cls:'f-w'},x.label),
      el('i',{cls:'f-d'},mdFrag(x.d)));
    // any_region の値は部位を選んでから入れる
    if(x.kind==='attr_value' && x.extra.attr.applies_kind==='any_region'){
      const mine=(s?(s.regions||[]):[]).map(r=>({slug:r}));
      const rs=opt(mine.concat((V.region||[]).filter(r=>!mine.some(m=>m.slug===r.slug))),
                   '', 'どこに (必須)');
      rs.addEventListener('change',()=>{x.__reg=rs.value});
      row.append(rs);
    }
    // 0072: 型は**検索の単位**でもある — 分解された所見を 1 語で引く。
    // 左右は照合しない (右手左手の指定には限界がある、user 指摘)
    if(x.kind==='macro'){
      const out2=el('span',{cls:'hint'});
      row.append(el('button',{cls:'sub',on:{click:async()=>{
        out2.textContent=' 探しています…';
        try{ const r=await (await fetch(U('/api/pattern/')+x.slug)).json();
             out2.replaceChildren('  '+(r.n??(r.posts||[]).length)+' 件 ',
               ...(r.posts||[]).slice(0,12).map(pid=>el('a',{cls:'k',href:'#',
                 on:{click:e=>{e.preventDefault(); location.hash='post='+pid}}},pid+' ')));
        }catch(e){ out2.textContent=' 引けませんでした' }
      }}},'この形の画像を探す'), out2);
      if(x.extra.tag) row.append(el('span',{cls:'hint'},'従来タグ: '+x.extra.tag));
    }
    const add=findAdder(x,s);
    if(add) row.append(el('button',{cls:'sub',on:{click:add}},'入れる'));
    out.append(row);
  });
}
// **入れられるものだけボタンを出す。** 出しておいて何も起きないのが一番悪い
function findAdder(x,s){
  if(!s) return null;
  const bump=()=>{render(); runFind()};
  if(x.kind==='region')  return ()=>{s.regions=s.regions||[];
    if(!s.regions.includes(x.slug))s.regions.push(x.slug); bump()};
  if(x.kind==='pose')    return ()=>{s.poses=s.poses||[];
    if(!s.poses.some(p=>p.pose===x.slug))s.poses.push({pose:x.slug}); bump()};
  if(x.kind==='attr_value'){ const a=x.extra.attr, v=x.extra.val;
    return ()=>{s.attributes=s.attributes||[];
      // any_region は**どこに付いているかが本題**なので部位が要る (0065)。
      // 部位なしで入れると「泡がある」だけの、位置を言わない所見になる。
      let reg=a.applies_kind==='region'?a.applies_key:null;
      if(a.applies_kind==='any_region'){
        reg=x.__reg||'';
        if(!reg){ alert('この属性は部位を選んでください (どこに付いているか)'); return }
      }
      if(reg){s.regions=s.regions||[]; if(!s.regions.includes(reg))s.regions.push(reg)}
      const cur=s.attributes.find(y=>y.attribute===a.slug&&(y.region||null)===(reg||null));
      if(cur) cur.value=v.slug; else s.attributes.push(
        Object.assign({attribute:a.slug,value:v.slug}, reg?{region:reg}:{}));
      bump()}}
  if(x.kind==='object')  return ()=>{s.objects=s.objects||[];
    s.objects.push({object:x.slug}); bump()};
  if(x.kind==='species') return ()=>{s.identity=s.identity||{}; s.identity.species=x.slug; bump()};
  if(x.kind==='descriptor') return ()=>{ann.scene_descriptors=ann.scene_descriptors||[];
    if(!ann.scene_descriptors.includes(x.slug))ann.scene_descriptors.push(x.slug); bump()};
  if(x.kind==='relation') return ()=>{ann.relations.push({relation:x.slug,
    subject:{ref:s.ref,region:''},object:{ref:'',region:''}}); bump()};
  // **主体は「選択中の人物」。** 常に subjects[0] を $s にしていた版は、
  // person_2 を選んで羽交い締めを入れると **person_1 が拘束する側**になり、
  // 関係が丸ごと逆になった (user 指摘 2026-07-31、私の入れたバグ)。
  if(x.kind==='macro') return ()=>{
    const other=(ann.subjects.find(y=>y.ref&&y.ref!==s.ref)||{}).ref;
    $('say').value=s.ref+' '+x.name+(other?' '+other:'');
    applySay(); runFind()};
  return null;   // 属性そのもの (値を選ばないと事実にならない) と環境は入れない
}
// 0079 後: **確定済みの所見を producer で色分けして見せる** (user 指摘 2026-07-31)。
// 下書き (ann、作業中) とは別に DB の確定所見を出す。arm M ではモデルの所見が
// ここに出て、人間は誤りを撤回し欠落を足す。
async function loadObsReview(){
  const box=$('obsreview'); if(!box||!cur) return;
  box.textContent='読み込み中…';
  let d; try{ d=await (await fetch(U('/api/scene_obs/')+cur)).json() }
  catch(e){ box.textContent='読めませんでした'; return }
  const line=(o,who)=>{
    const row=el('div',{cls:'obs-line obs-'+who});
    row.append(el('em',{cls:'obs-tag obs-'+who},who==='model'?'モデル':'人間'),
               el('span',{},o.label));
    // モデルの所見に **採用(✓) / 不採用(✗)** を 1 文字アイコンで (user 指摘 2026-07-31)。
    if(who==='model'){
      row.append(
        el('button',{cls:'obs-ic ok',title:'採用 — 正しい (人間名義でも記録)',
          on:{click:()=>adoptObs(o.id)}},'✓'),
        el('button',{cls:'obs-ic no',
          title:'不採用 — 誤りとして撤回。正しい値があれば右で入れれば上書き (種別は event から導く)',
          on:{click:()=>rejectObs(o.id, o.label)}},'✗'));
    }
    return row;
  };
  // 右パネルの重ね合わせ用に**モデルの選択を ref・軸で索引化** (user 指摘 2026-07-31)。
  // **人間・モデル両方**を ref・軸で索引化 — 各コントロール脇の突合チップ用
  // (user 指摘 2026-07-31: 突合をコントロールへ統合)。id を持つのは
  // チップから直接 ✓採用/✗不採用 するため。
  const obsIndex=rows=>{
    // **同値は 1 チップにまとめる** — model 観測は世代 (producer_ref) をまたいで
    // 生きているので、@base と @s0 が同じ値を書くと素通しでは二重に出る。
    // 全世代の id を ids に集める — ✗ は**全世代をまとめて撤回**しないと
    // 値が生き残ってもう 1 クリック要る (2026-08-01、実使用の二重撤回で発覚)。
    const acc=(map,key,o)=>{const p=map.get(key);
      map.set(key,Object.assign({},o,{ids:p?p.ids.concat(o.id):[o.id]}))};
    const rels=new Map(), descs=new Map(), ix={};
    rows.forEach(o=>{
      if(o.axis==='relation'){acc(rels,o.val,o);return}
      if(o.axis==='descriptor'){acc(descs,o.val,o);return}
      const r=o.ref||'_';
      const e=ix[r]=ix[r]||{regions:new Map(),poses:new Map(),attrs:new Map()};
      if(o.axis==='species'||o.axis==='presentation'){
        const p=e[o.axis];
        e[o.axis]=Object.assign({},o,
          {ids:(p&&p.val===o.val)?p.ids.concat(o.id):[o.id]});
      }
      else if(o.axis==='region') acc(e.regions,o.val,o);
      else if(o.axis==='pose') acc(e.poses,o.val,o);
      else if(o.axis==='attribute'){
        const i=o.val.indexOf('=');
        const key=o.val+'@'+(o.region||''), p=e.attrs.get(key);
        e.attrs.set(key,{attribute:o.val.slice(0,i),value:o.val.slice(i+1),
           region:o.region||'',id:o.id,label:o.label,
           ids:p?p.ids.concat(o.id):[o.id]});
      }
    });
    ix._rels=[...rels.values()]; ix._descs=[...descs.values()];
    return ix;
  };
  MODELOBS=obsIndex(d.model);
  HUMANOBS=obsIndex(d.human);
  render();   // チップを全コントロール (人物カード・関係・構図) に反映
  const nm=d.model.length, nh=d.human.length;
  box.replaceChildren();
  if(!nm && !nh){ box.textContent='まだ確定済みの所見はありません'; return }
  box.append(el('div',{cls:'hint'},`モデル ${nm} 件 / 人間 ${nh} 件`));
  d.model.forEach(o=>box.append(line(o,'model')));
  d.human.forEach(o=>box.append(line(o,'human')));
}
// ✓✗ は**すぐ書かない** — 5 秒の取り消し猶予を置いてから POST する
// (user 要望 2026-08-01「誤操作でチェックしたときに戻したい」。DB は append-only で、
//  書いた後の取り消しは撤回の連鎖になる — 書く前に止めるのが構造的に安い)。
// 猶予中に別の ✓✗ を押したら前のは確定してから次を積む。ページ離脱時も確定
// (POST は keepalive なので離脱中でも届く)。
let PENDING=null;
function flushPending(){ if(PENDING){const p=PENDING;PENDING=null;
  clearTimeout(p.t);p.box.remove();p.run()} }
addEventListener('pagehide',flushPending);
function deferObs(msg,run){
  flushPending();
  const box=el('div',{cls:'undo-toast'},msg,
    el('button',{cls:'sub',on:{click:()=>{clearTimeout(PENDING.t);PENDING=null;
      box.remove();stat('取り消しました','var(--warn)')}}},'元に戻す'));
  document.body.append(box);
  PENDING={box,run,t:setTimeout(()=>{PENDING=null;box.remove();run()},5000)};
}
function adoptObs(oid){ deferObs('採用します…',()=>adoptNow(oid)) }
async function adoptNow(oid){
  try{
    const r=await (await fetch(U('/api/adopt'),{method:'POST',keepalive:true,
      body:JSON.stringify({observation:oid, annotator:'desk'})})).json();
    if(r.ok){ stat('採用しました','var(--acc)'); loadObsReview() }
    else stat('採用できません: '+(r.error||''),'var(--err)');
  }catch(e){ stat('採用に失敗','var(--err)') }
}
// ✗ = 1 タップで撤回 (retraction event)。理由の種別は**選ばせない** —
// 「同じ要素に置き換えの human 観測があるか」で削除型/上書き型を F0 が event から導く
// (user 指摘 2026-07-31「特別な作業フローを用意しなくてもイベント記録を取れる」)。
function rejectObs(oid,label){ deferObs('不採用にします…',()=>rejectNow(oid,label)) }
async function rejectNow(oid, label){
  // oid は単一 id か id 配列 (同値の全世代)。全部撤回しないと値が生き残る
  try{
    let ok=true;
    for(const id of [].concat(oid)){
      const r=await (await fetch(U('/api/retract'),{method:'POST',keepalive:true,
        body:JSON.stringify({observation:id, why:'不採用: '+(label||'')})})).json();
      if(!r.ok){ ok=false; stat('不採用にできません: '+(r.error||''),'var(--err)') }
    }
    if(ok){ stat('不採用にしました','var(--warn)') }
    loadObsReview();
  }catch(e){ stat('不採用に失敗','var(--err)') }
}
async function retractObs(oid, label){
  const why=prompt('撤回の理由 (なぜ誤りか)。空なら中止:', '');
  if(!why) return;
  try{
    const r=await (await fetch(U('/api/retract'),{method:'POST',
      body:JSON.stringify({observation:oid, why:why})})).json();
    if(r.ok){ stat('撤回しました','var(--acc)'); loadObsReview(); }
    else stat('撤回できません: '+(r.error||''),'var(--err)');
  }catch(e){ stat('撤回に失敗','var(--err)') }
}
// モデルが挙げた部位を人間の選択と突き合わせて出す (user 指摘 2026-07-31)。
// 一致 = 緑、モデルのみ (あなたが未選択) = 橙で「＋」= ワンタップで下書きに採る。
// モデルの選択集合を人間の集合と突き合わせて出す汎用版 (ポーズ・属性)。
// adder があればモデルのみをクリックで下書きに採れる (属性は値・部位が絡むので採らせない)。
// ── 突合チップ (user 指摘 2026-07-31「コントロールと突合が離れすぎ」) ──
// DB の確定所見を**各コントロールの真横**に出す。人=青 (情報)、モ=一致なら緑・
// 食い違いなら橙。橙のモには右パネルと同じ ✓採用(人間名義で記録)/✗不採用(撤回) を
// 直接付け、チップ本体のクリックは**下書きに採るだけ** (まだ確定しない)。
// 語彙のディスクリプションを tooltip 用に引く (属性は [slug, value] で値の判定基準も)
// tooltip 用の短い説明: 平文化し、[category] 前置きを落とし、最初の一文・60 字まで
// (user 指摘 2026-08-01「長すぎる。簡潔に」)
function tipShort(x,max){
  max=max||60;
  let s=mdPlain(String(x||'')).split('\n')[0].replace(/^\[[^\]]*\]\s*/,'');
  const i=s.indexOf('。');
  if(i>=0) s=s.slice(0,i+1);
  return s.length>max?s.slice(0,max)+'…':s;
}
function vdesc(axis,dv){
  const first=tipShort;
  const find=(l,v)=>{const m=norm(l||[]).find(x=>x.slug===v);return m?first(m.d):''};
  if(axis==='attribute'&&Array.isArray(dv)){
    const m=(V.attribute||[]).find(x=>x.slug===dv[0]); if(!m) return '';
    const vm=(m.values||[]).find(x=>x.slug===dv[1]);
    return [first(m.d), vm&&vm.d?('値 '+dv[1]+': '+tipShort(vm.d,42)):'']
      .filter(Boolean).join('\n');
  }
  return find({species:V.species,region:V.region,pose:V.pose,
               relation:V.relation,descriptor:V.descriptor}[axis], dv);
}
const _vs=dv=>Array.isArray(dv)?dv[0]+'='+dv[1]:String(dv??'');
// 一致印: 下書きに既にある値には突合チップを再掲せず、既存のタグ・行に付ける
// (user 指摘 2026-08-01「人 areola✓ と再び打つのは行の無駄」)
function hmMarks(axis,dv,hu,mo){
  const d=vdesc(axis,dv), out=[];
  if(hu) out.push(el('i',{cls:'hm-m hu',
    title:'人間の確定所見と一致: '+_vs(dv)+(d?'\n'+d:'')},'人'));
  if(mo) out.push(el('i',{cls:'hm-m mo',
    title:'モデルの所見と一致: '+_vs(dv)+(d?'\n'+d:'')},'モ'));
  return out;
}
function huChip(o,text,matched,axis,dv){
  const d=vdesc(axis,dv!==undefined?dv:text);
  return el('span',{cls:'mchip hu','data-axis':axis||'',
    title:'人間の確定所見'+(text?'':': '+_vs(dv))+(matched?' — 下書きと一致':'')
      +(d?'\n'+d:'')},
    text?('人 '+text+(matched?' ✓':'')):'人✓');
}
function moChip(o,text,matched,toDraft,axis,dv){
  const d=vdesc(axis,dv!==undefined?dv:text);
  const chip=el('span',{cls:'mchip '+(matched?'ok':'bad'),'data-axis':axis||'',
    title:'モデルの所見'+(text?'':': '+_vs(dv))+(matched?' — 一致'
      :(toDraft?' — 本体クリックで下書きに採る':''))+(d?'\n'+d:'')},
    text?('モ '+text+(matched?' ✓':'')):'モ✓');
  if(!matched){
    // 本体クリックだけ下書きに採る。✓✗ の <i> は自分で止めるが、
    // target 検査でも守る (誤爆で DB に書かない)
    if(toDraft) chip.addEventListener('click',e=>{if(e.target!==chip)return;toDraft()});
    chip.append(
      el('i',{cls:'mic ok',title:'採用 — 正しい (人間名義でも記録)',
        on:{click:e=>{e.stopPropagation();adoptObs(o.id)}}},'✓'),
      el('i',{cls:'mic no',title:'不採用 — 誤りとして撤回 (同値の全世代をまとめて)',
        on:{click:e=>{e.stopPropagation();rejectObs(o.ids||[o.id],o.label)}}},'✗'));
  }
  return chip;
}
function hmRow(chips){
  const c=chips.filter(Boolean);
  return c.length?el('div',{cls:'morow'},el('em',{cls:'moh'},'突合'),...c):'';
}
// 単値軸 (species / presentation): select の脇に置くチップ列
function hmValChips(ref,axis,getDraft,setDraft){
  const h=(HUMANOBS[ref]||{})[axis], m=(MODELOBS[ref]||{})[axis];
  if(!h&&!m) return [];
  // 一致は値を繰り返さず 人✓/モ✓ に圧縮 (値は select に見えている。tooltip には残す)
  const dv=getDraft(), chips=[];
  if(h) chips.push(huChip(h,dv===h.val?'':h.val,dv===h.val,axis,h.val));
  if(m){const mm=dv===m.val||!!(h&&h.val===m.val);
    chips.push(moChip(m,mm&&dv===m.val?'':m.val,mm,
                      ()=>{setDraft(m.val);render()},axis,m.val));}
  return chips;
}
// 集合軸 (region / pose): grpSet に属する値だけを、そのカテゴリの箱に出す
function hmSetRow(ref,axis,grpSet,draftSet,toDraft){
  // 下書きに既にある値はタグ側の一致印に任せ、ここには出さない (行の無駄)
  const H=((HUMANOBS[ref]||{})[axis+'s'])||new Map();
  const M=((MODELOBS[ref]||{})[axis+'s'])||new Map();
  const chips=[];
  [...H.keys()].filter(v=>grpSet.has(v)&&!draftSet.has(v)).sort().forEach(v=>
    chips.push(huChip(H.get(v),v,false,axis,v)));
  [...M.keys()].filter(v=>grpSet.has(v)&&!draftSet.has(v)).sort().forEach(v=>
    chips.push(moChip(M.get(v),v,H.has(v),()=>toDraft(v),axis,v)));
  return hmRow(chips);
}
// 属性: rgSet に属する region の属性 (rgSet='' は region 無しの全体属性) を出す
function hmAttrRow(s,rgSet){
  const pick=ix=>[...(((ix[s.ref]||{}).attrs)||new Map()).values()]
      .filter(a=>rgSet==='' ? !a.region : rgSet.has(a.region));
  const dHas=a=>(s.attributes||[]).some(x=>x.attribute===a.attribute
      &&String(x.value??'')===(a.value==='-'?'':a.value)
      &&String(x.region||'')===String(a.region||''));
  const hu=pick(HUMANOBS), mo=pick(MODELOBS);
  const key=a=>a.attribute+'='+a.value+'@'+a.region;
  const hukeys=new Set(hu.map(key));
  const text=a=>a.attribute+'='+a.value+(a.region?' @'+a.region:'');
  const chips=[];
  // 下書き済みは属性行側の一致印に任せる
  hu.filter(a=>!dHas(a)).forEach(a=>
    chips.push(huChip(a,text(a),false,'attribute',[a.attribute,a.value])));
  mo.filter(a=>!dHas(a)).forEach(a=>chips.push(moChip(a,text(a),hukeys.has(key(a)),
      a.value!=='-'?()=>{
        const e={attribute:a.attribute,value:a.value};
        if(a.region){e.region=a.region;
          if(!s.regions.includes(a.region))s.regions.push(a.region)}
        s.attributes.push(e);render()}:null,'attribute',[a.attribute,a.value])));
  return hmRow(chips);
}
// 下書きの属性 1 行に対する一致印 (値・部位まで一致したときだけ)
function attrMarks(ref,a){
  const has=ix=>[...(((ix[ref]||{}).attrs)||new Map()).values()].some(x=>
    x.attribute===a.attribute&&x.value===String(a.value??'')
    &&String(x.region||'')===String(a.region||''));
  return hmMarks('attribute',[a.attribute,a.value||''],has(HUMANOBS),has(MODELOBS));
}
function renderSubjects(){
  $('subjects').replaceChildren(...ann.subjects.map((s,i)=>subjCard(s,i)));
}
function render(){
  // 人物 (右パネル。カテゴリは分断しない)
  renderSubjects();
  // 関係
  // 「誰の」は人物への参照なので、画像上の印と同じ色を持たせる
  const refs=ann.subjects.map((s,i)=>({slug:s.ref, c:subjColor(i)})).filter(x=>x.slug);
  const refColor=ref=>{const i=ann.subjects.findIndex(s=>s.ref===ref);
                       return i<0?null:subjColor(i)};
  const isEmis=r=>(V.emission_rels||[]).includes(r.relation);
  // 関係の突合 (scene 単位)。下書き済みの関係は行側に一致印を付け、チップは
  // 未取り込み分だけ (user 指摘 2026-08-01: 再掲は行の無駄)
  const relKey=r=>[r.relation,(r.subject||{}).ref||'',(r.subject||{}).region||'',
                   (r.object||{}).ref||'',(r.object||{}).region||''].join('|');
  const parseRel=v=>{const m=v.match(/^(.*?)\/(.*?)>(.*?)>(.*?)\/(.*)$/)||[];
    const cl=x=>(!x||x==='-')?'':x;
    return {relation:cl(m[3]),subject:{ref:cl(m[1]),region:cl(m[2])},
            object:{ref:cl(m[4]),region:cl(m[5])}}};
  const relMine=new Set(ann.relations.map(relKey));
  const relHu=new Set(HUMANOBS._rels.map(o=>relKey(parseRel(o.val))));
  const relMo=new Set(MODELOBS._rels.map(o=>relKey(parseRel(o.val))));
  const mkRelRow=(r,i,only)=>{
    const row=relRow(r,i,refs,refColor,only);
    const k=relKey(r);
    row.append(...hmMarks('relation',r.relation,relHu.has(k),relMo.has(k)));
    return row;
  };
  $('rels').replaceChildren(...ann.relations.map((r,i)=>
      isEmis(r)?null:mkRelRow(r,i,null)).filter(Boolean));
  // 噴出物の関係は専用の箱へ (0062)。**同じ行の作り方を使い回す** — 2 通りの行を
  // 別々に書くと、片方だけ直したときに差が出る (左右の select を 1 度そうして壊した)。
  $('emis-r').replaceChildren(...ann.relations.map((r,i)=>
      isEmis(r)?mkRelRow(r,i,V.emission_rels):null).filter(Boolean));
  $('emis-a').replaceChildren(emisAttrs());
  const relTxt=o=>o.label.replace(/^関係 /,'');
  const rchips=[];
  HUMANOBS._rels.filter(o=>!relMine.has(relKey(parseRel(o.val)))).forEach(o=>
    rchips.push(huChip(o,relTxt(o),false,'relation',parseRel(o.val).relation)));
  MODELOBS._rels.forEach(o=>{const pr=parseRel(o.val), k=relKey(pr);
    if(relMine.has(k)) return;
    rchips.push(moChip(o,relTxt(o),relHu.has(k),
      ()=>{ann.relations.push(pr);render()},'relation',pr.relation))});
  $('relobs').replaceChildren(hmRow(rchips)||'');
  sayWho();
  // **語数は数えて出す。** 固定文で書いていた版は語彙が増えても直らず、
  // 画面が古い数字を主張し続けた (0066 で部位が 129 -> 133 になった)
  const vc=$('vcount');
  if(vc) vc.textContent='全語彙 '+((FIND_IX=FIND_IX||findIndex()).length)+' 語';
  // 画面全体
  // 幅と高さで決まる事実は**注釈させず**、計算結果を見せるだけ (保存もしない)
  const wd=DERIVED||{};
  const dparts=[];
  if(wd.orientation) dparts.push(`<b class="k">画面の向き = ${wd.orientation}</b>`
    + ` <span class="hint">(${wd.width}×${wd.height} px)</span>`);
  if(wd.color_mode) dparts.push(`<b class="k">色モード = ${wd.color_mode}</b>`
    + ` <span class="hint">(${wd.color_why})</span>`);
  else if(wd.color_why) dparts.push(`<span class="hint">色モード = 判定せず (${wd.color_why})</span>`);
  $('derived').innerHTML = dparts.length
    ? '自動判定 — 注釈しません:<br>' + dparts.map(x=>'・'+x).join('<br>')
    : '<span class="hint">画素が未計測のため自動判定できません</span>';
  $('descs').replaceChildren(chipsGrouped(ann.scene_descriptors,V.descriptor,render));
  // 構図の突合 (scene 単位)。下書き済みはタグに一致印、チップは未取り込み分だけ
  const dMine=new Set(ann.scene_descriptors||[]);
  const dHu=new Set(HUMANOBS._descs.map(o=>o.val));
  const dMo=new Set(MODELOBS._descs.map(o=>o.val));
  $('descs').querySelectorAll('.tag').forEach(t=>{
    const v=((t.firstChild&&t.firstChild.textContent)||'').trim();
    if(dHu.has(v)||dMo.has(v))
      hmMarks('descriptor',v,dHu.has(v),dMo.has(v))
        .forEach(m=>t.insertBefore(m,t.lastChild));
  });
  const dchips=[];
  HUMANOBS._descs.filter(o=>!dMine.has(o.val)).forEach(o=>
    dchips.push(huChip(o,o.val,false,'descriptor',o.val)));
  MODELOBS._descs.filter(o=>!dMine.has(o.val)).forEach(o=>
    dchips.push(moChip(o,o.val,dHu.has(o.val),
      ()=>{ann.scene_descriptors.push(o.val);render()},'descriptor',o.val)));
  $('descobs').replaceChildren(hmRow(dchips)||'');
  $('counts').replaceChildren(...ann.entity_counts.map((c,i)=>{
    const p=opt(V.presentation,c.presentation,'presentation');
    const n=el('input',{type:'number',min:'0',value:c.count_min,style:'width:64px'});
    const ex=el('input',{type:'checkbox'}); ex.checked=c.count_exact!==false;
    p.onchange=()=>c.presentation=p.value; n.oninput=()=>c.count_min=parseInt(n.value||'0');
    ex.onchange=()=>c.count_exact=ex.checked;
    // 0079: **背景の観衆を個体にしないための欄。** 参加者と傍観者が同じ数に混ざると
    // 人数の問い (A4) も持ち主の問い (A6) も歪む (user 指摘 2026-07-31、post 378469)。
    // 空欄 = 言っていない
    const iv=opt([{slug:'participant',n:'行為に加わっている'},
                  {slug:'bystander',  n:'見ているだけ (背景)'}],
                 c.involvement||'', '参加/傍観 (任意)');
    iv.addEventListener('change',()=>{c.involvement=iv.value||undefined});
    return el('div',{cls:'row'},p,n,el('label',{style:'margin:0'},ex,' 正確な人数'),iv,
      el('button',{cls:'del',on:{click:()=>{ann.entity_counts.splice(i,1);render()}}},'×'));
  }));
  // unresolved
  $('unres').replaceChildren(...ann.unresolved.map((u,i)=>{
    const d=opt(['attribute','body_region','pose'],u.domain,'domain');
    d.onchange=()=>{u.domain=d.value;render()};
    const row=el('div',{cls:'row'},d);
    if(u.domain==='attribute'){
      const meta=V.attribute.find(x=>x.slug===u.attribute);
      const a=opt(V.attribute.map(x=>({slug:x.slug,d:x.d})),u.attribute,'attribute');
      const v=opt(meta?meta.values:[],u.value,'値');
      a.onchange=()=>{u.attribute=a.value;delete u.value;render()}; v.onchange=()=>u.value=v.value;
      row.append(a,v);
    }else if(u.domain==='body_region'){
      const r=opt(V.region,u.region,'region'); r.onchange=()=>u.region=r.value; row.append(r);
    }else{
      const p=opt(V.pose,u.pose,'pose'); p.onchange=()=>u.pose=p.value; row.append(p);
    }
    const nt=el('input',{value:u.note||'',placeholder:'なぜ特定できないか',style:'min-width:220px;flex:1'});
    nt.oninput=()=>u.note=nt.value;
    row.append(nt,el('button',{cls:'del',on:{click:()=>{ann.unresolved.splice(i,1);render()}}},'×'));
    return row;
  }));
  drawBoxes();
  // 選択中の人物が変わったらバーの表示も追随させる
  const s=ann&&ann.subjects[act]; $('bbmwho').textContent=s?(s.ref||('#'+(act+1))):'人物なし';
}
// 人物ごとの色。**順番で決まる**ので、同じ下書きを開けば毎回同じ色になる。
// 暗い画像の上でも読めるよう明度を上げた色にし、隣り合う色相を離してある。
const SUBJ_COLORS=['#7aa8f0','#f0a050','#5fd68a','#e879c8','#f0d24a','#5fd6d6',
                   '#c08cf5','#f07a7a','#9ad14a','#8fa8c8'];
const subjColor=i=>SUBJ_COLORS[i%SUBJ_COLORS.length];

function drawBoxes(){
  const out=[];
  ann.subjects.forEach((s,i)=>{
    const c=subjColor(i), name=s.ref||('#'+(i+1));
    if(s.bbox){
      const[a,b,cc,d]=s.bbox;
      out.push(el('div',{cls:'bx'+(i===act?' act':''),
        style:`--c:${c};left:${a*100}%;top:${b*100}%;width:${(cc-a)*100}%;height:${(d-b)*100}%`},
        el('span',{},name)));
    }
    // 点は十字 + 輪。重なった人物でも別々の位置に立つので box より見分けが付く。
    // **どちらにも必ず名前を出す** — 色だけだと人物カードと突き合わせにくい。
    if(s.anchor){
      out.push(el('div',{cls:'pt'+(i===act?' act':''),
        style:`--c:${c};left:${s.anchor.x*100}%;top:${s.anchor.y*100}%`},
        el('i',{}), el('span',{},`${name} · ${anchorRegionOf(s.anchor)}`)));
    }
  });
  $('boxes').replaceChildren(...out);
}
// ── bbox 描画モード ──────────────────────────────────────
// 画像はパネルの裏まで広がっているので、通常時はそこをドラッグできない (2026-07-28 指摘)。
// モードを明示して、その間だけ画像を最前面に出す。状態はバーと枠の色で分かるようにする。
// 点が指す部位。**body_region そのもの** (0028)。頭だけでなく性器や腕も指せる。
let anchorRegion='head';
function anchorPicker(){
  const sel=opt(V.region, anchorRegion, '指す部位');
  sel.addEventListener('change',()=>{ if(sel.value) anchorRegion=sel.value });
  return sel;
}
function setBbm(on){
  document.body.classList.toggle('bbm',!!on);
  $('bbmbtn').classList.toggle('bbm-on',!!on);
  const s=ann&&ann.subjects[act];
  $('bbmwho').textContent=s?(s.ref||('#'+(act+1))):'人物なし';
  if(on) $('bbmpart').replaceChildren(anchorPicker());
}
addEventListener('keydown',e=>{
  if(e.target.closest('input,textarea')) return;
  if(e.key==='Escape') setBbm(false);
  else if(e.key==='b'||e.key==='B') setBbm(!document.body.classList.contains('bbm'));
});
// bbox ドラッグ
(function(){let st=null;const st_=$('stage');
 // 保険: draggable=false / user-drag を無視する経路が残っていても掴ませない
 st_.addEventListener('dragstart',e=>e.preventDefault());
 st_.addEventListener('pointerdown',e=>{if(e.target.tagName!=='IMG')return;
   if(!document.body.classList.contains('bbm'))return;   // モード中だけ描ける (誤描画防止)
   const r=$('im').getBoundingClientRect();
   st={x:(e.clientX-r.left)/r.width,y:(e.clientY-r.top)/r.height,r};
   st_.setPointerCapture(e.pointerId); e.preventDefault()});
 // move/up は **window** で拾う。setPointerCapture に頼ると、画像の外まで引っ張った
 // 瞬間に途切れて小さい矩形しか描けなかった (2026-07-28、右下へ引くと必ず起きる)。
 addEventListener('pointermove',e=>{if(!st||!ann.subjects[act])return;
   const x=(e.clientX-st.r.left)/st.r.width,y=(e.clientY-st.r.top)/st.r.height;
   // 動きが小さいうちは「クリック (点)」のつもりとみなし、bbox を作らない
   if(Math.abs(x-st.x)<0.02&&Math.abs(y-st.y)<0.02) return;
   st.dragged=true;
   const cl=v=>Math.max(0,Math.min(1,v));
   ann.subjects[act].bbox=[cl(Math.min(st.x,x)),cl(Math.min(st.y,y)),cl(Math.max(st.x,x)),cl(Math.max(st.y,y))];
   drawBoxes()});
 // 描き終えたらモードを抜ける — 出しっぱなしだとパネルが触れないままになる
 const end=()=>{if(!st)return;
   // ドラッグしていなければ**点**を置く (1 クリック)。頭の中心が既定。
   if(!st.dragged&&ann.subjects[act]){
     const cl=v=>Math.max(0,Math.min(1,v));
     ann.subjects[act].anchor={x:cl(st.x),y:cl(st.y),region:anchorRegion};
   }
   st=null; setBbm(false); render(); touch()};
 addEventListener('pointerup',end); addEventListener('pointercancel',end);
})();
// ── 自動保存 ──────────────────────────────────────────────
// 「全件いきなり確定できないがリロードすると状態が消える」への対処 (2026-07-27)。
// 変更のたびに 700ms 後へ debounce して下書きを PUT する。確定 (DB 書き込み) とは別で、
// 下書きは何度でも上書きできる (観測と違い append-only ではない)。
let saveT=null, dirty=false;
function stat(t,c){const e=$('st'); e.textContent=t; e.style.color=c||'var(--dim)'}
function touch(){dirty=true; stat('未保存','var(--warn)');
  clearTimeout(saveT); saveT=setTimeout(()=>draft(),700)}
// 注釈時間は**測れる**ので人に入力させない (向き・色モードと同じ原則)。
// 画面が見えている間だけ積算する。手で上書きしたい場合は分の欄を直接編集できる。
let activeMs=0, activeAt=null;
function tick(){ if(activeAt!==null&&document.visibilityState==='visible'){
  const now=performance.now(); activeMs+=now-activeAt; activeAt=now } }
setInterval(()=>{ if(!ann) return; tick();
  if(!minutesTouched){ const m=Math.round(activeMs/6000)/10;
    if(m>0&&m!==parseFloat($('minutes').value)){ $('minutes').value=m;
      if($('gobtn').classList.contains('blocked')) checkReady() } } }, 5000);
document.addEventListener('visibilitychange',()=>{
  if(document.visibilityState==='visible') activeAt=performance.now();
  else { tick(); activeAt=null } });
let minutesTouched=false;
async function draft(){
  if(!ann) return;
  tick(); ann.seconds_active=Math.round(activeMs/1000);
  if(!minutesTouched&&activeMs>0) $('minutes').value=Math.round(activeMs/6000)/10;
  ann.minutes=$('minutes').value===''?null:parseFloat($('minutes').value);
  ann.notes=$('notes').value; ann.gaps=$('gaps').value;
  pruneEmpty(ann);
  if(RO){ dirty=false; stat('読むだけ (?ro=1) — 下書きは保存しません','var(--warn)'); return }
  try{ await fetch(U('/api/draft'),{method:'PUT',body:JSON.stringify(ann)});
       dirty=false; stat('保存済み','var(--acc)'); checkReady() }
  catch(e){ stat('保存失敗','var(--err)') }
}
// **押すまで確定できるか判らない**のを直す (2026-07-28)。保存のたびに検証だけ走らせ、
// 残っている障害を出す。確定ボタンの見た目も連動させる。
async function checkReady(){
  if(!ann) return;
  // 検証にかける前に、画面の値を ann へ写す。写さないと『分は入っているのに
  // minutes が無いと言われる』という食い違いが出る (2026-07-28)。
  tick(); ann.seconds_active=Math.round(activeMs/1000);
  if(!minutesTouched&&activeMs>0) $('minutes').value=Math.max(0.1,Math.round(activeMs/6000)/10);
  ann.minutes=$('minutes').value===''?null:parseFloat($('minutes').value);
  ann.notes=$('notes').value; ann.gaps=$('gaps').value;
  try{
    const r=await (await fetch(U('/api/validate'),{method:'POST',body:JSON.stringify(ann)})).json();
    const n=(r.errors||[]).length, go=$('gobtn');
    go.disabled=false;
    if(!n){ go.textContent='確定して次へ'; go.classList.remove('blocked');
            $('ready').innerHTML='<span class="k">確定できます</span>' }
    else{ go.textContent='確定 ('+n+' 件の問題)'; go.classList.add('blocked');
          $('ready').innerHTML='<span class="e">未確定 '+n+' 件:</span> '
            + r.errors.slice(0,3).map(e=>e.split(' — ')[0]).join(' / ')
            + (n>3?' ほか':''); }
  }catch(e){}
}
// フォーム操作をすべて拾う (個別のハンドラを足さなくても効く)
// **文書全体で拾う。** かつて panel は main の中にあったが、オーバーレイ化で外へ
// 出たため `closest('main,header')` が全て偽になり、**自動保存が丸ごと死んでいた**
// (2026-07-28 発見。下書きは「追加分を確定」を押したときしか書かれていなかった)。
// 除外するのは combobox の絞り込み欄だけ — これは注釈内容ではない。
const noSave=e=>e.target.closest('.cb-search')||e.target.classList.contains('cb-search');
document.addEventListener('input',e=>{if(!noSave(e))touch()},true);
document.addEventListener('change',e=>{if(!noSave(e))touch()},true);
// 離脱時は sendBeacon (fetch は間に合わないことがある)
addEventListener('beforeunload',()=>{ if(!dirty||!ann) return;
  ann.minutes=$('minutes').value===''?null:parseFloat($('minutes').value);
  ann.notes=$('notes').value; ann.gaps=$('gaps').value;
  if(RO) return;
  navigator.sendBeacon('/api/draft',new Blob([JSON.stringify(ann)],{type:'application/json'})) });

// 「＋」で作った空の器 (attribute='' / relation='' 等) は**保存前に落とす**。
// 残すと確定時に「ontology に無い」で弾かれるが、それは注釈者の誤りではなく
// 途中の UI 状態にすぎない。仕組みで作れないようにする方が正しい (2026-07-28)。
// 0079: **空文字を下書きに残さない。** 「選んでいない」は '' で届くので、
// そのまま保存すると DB 側で `object_surface_slug=''` が「環境を指定した」に、
// `subject_region_slug=''` が FK 違反になる (2026-07-31、下書き 6 件が確定できなかった)。
// **NULL は「言っていない」**という既定を守るには入口で落とすしかない。
function dropEmptyStrings(o){
  if(Array.isArray(o)){ o.forEach(dropEmptyStrings); return o }
  if(o && typeof o==='object'){
    Object.keys(o).forEach(k=>{
      if(typeof o[k]==='string' && o[k].trim()==='') delete o[k];
      else dropEmptyStrings(o[k]);
    });
  }
  return o;
}
function pruneEmpty(a){
  dropEmptyStrings(a);
  a.subjects.forEach(s=>{
    s.attributes=(s.attributes||[]).filter(x=>x.attribute&&(x.value||x.numeric!=null));
    s.poses=(s.poses||[]).filter(x=>(typeof x==='string')?x:x.pose);
    s.objects=(s.objects||[]).filter(x=>x&&x.object);
  });
  a.relations=(a.relations||[]).filter(r=>r.relation);
  a.entity_counts=(a.entity_counts||[]).filter(x=>x.presentation);
  // unresolved は domain ごとに要る欄が違う。中途半端な行を残すと確定時に落ちる。
  a.unresolved=(a.unresolved||[]).filter(x=>
    x.domain==='attribute' ? x.attribute :
    x.domain==='body_region' ? x.region :
    x.domain==='pose' ? x.pose : false);
  a.subjects=a.subjects.filter(s=>s.ref);
  return a;
}
async function save(commit){
  // 5 秒周期の自動記入を待たずに確定できるよう、その場で計時から埋める
  tick(); ann.seconds_active=Math.round(activeMs/1000);
  if(!minutesTouched&&activeMs>0) $('minutes').value=Math.max(0.1,Math.round(activeMs/6000)/10);
  ann.minutes=$('minutes').value===''?null:parseFloat($('minutes').value);
  ann.notes=$('notes').value; ann.gaps=$('gaps').value;
  pruneEmpty(ann);
  ann.subjects.forEach(s=>{if(s.bbox&&(s.bbox[2]-s.bbox[0]<0.01||s.bbox[3]-s.bbox[1]<0.01))s.bbox=null});
  clearTimeout(saveT); await draft();
  const r=await (await fetch(U('/api/commit'),{method:'POST',body:JSON.stringify(ann)})).json();
  if(r.errors&&r.errors.length){
    // **失敗を目立たせる。** メッセージ欄だけに出していた版は、
    // 「押しても何も起きない」ようにしか見えなかった (user 指摘 2026-07-31)。
    $('msg').innerHTML='<span class="e">'+r.errors.length+' 件の問題:</span>\n'+
      r.errors.map(e=>'  - '+e).join('\n')+'\n\n何も投入していません。';
    const st=$('cerr');
    if(st){ st.hidden=false; st.textContent='確定できません ('+r.errors.length+' 件) — '
              + r.errors[0].slice(0,60) + ' … ここを押すと詳細へ';
            st.onclick=()=>{$('msg').scrollIntoView({block:'center'})} }
    const gb=$('gobtn');
    if(gb){ gb.classList.add('blocked'); gb.textContent='確定できません — 直してから';
            setTimeout(()=>{gb.classList.remove('blocked');
                            gb.textContent='確定して次へ'}, 6000) }
    return {ok:false, errors:r.errors};
  }else{
    const w=r.written;
    $('msg').innerHTML='<span class="k">確定しました</span> — 新規 observation '+w.observation+
      ' / bbox '+w.asset_region+' / 関係 '+w.relation+' / unresolved '+w.unresolved+
      (w.skipped?('\n既に入っていて<b>飛ばした</b>もの '+w.skipped+' 件 (二重にはなりません)'):'');
    const st=$('cerr'); if(st) st.hidden=true;
    await reloadWL(); drawNav();
    return {ok:true, written:w};
  }
}
// 確定できたら層の順で次の未着手へ送る。詰まったらその場に留めて理由を出す。
async function commitAndNext(){
  // **確定が成功したかを save() から直接受け取る。** 一覧を取り直して
  // `written>0` を見ていた版は、(a) 取り直しに失敗すると前へ進めず、
  // (b) 既に観測がある scene なら確定が失敗しても進んでしまう、という
  // 二重の誤りがあった (user 指摘 2026-07-31)。判定の根拠は 1 つにする。
  const r=await save(1);
  if(r&&r.ok) nextTodo(cur);
}
// **古い版で操作していないかを画面が自分で見る** (user 指摘 2026-07-31
// 「リロードも効かない。ハードリロードなら効果がある」)。
// HTML は no-store なので普通は起きないが、起きたときに推測で片付けないため。
const BUILD_AT_LOAD='__BUILD__';
async function checkBuild(){
  try{
    const r=await (await fetch(U('/api/build'),{cache:'no-store'})).json();
    const el0=$('stale'); if(!el0) return;
    if(r.build && r.build!==BUILD_AT_LOAD){
      el0.hidden=false;
      el0.textContent='サーバのコードが新しくなっています — ここを押して読み直す';
      el0.onclick=()=>location.reload(true);
    }else el0.hidden=true;
  }catch(e){}
}
setInterval(checkBuild, 60000);
boot().then(checkBuild);
</script></body></html>
"""


# ── 補完ページ ────────────────────────────────────────────────
# S-11 が要求するのに注釈されていない 2 項目 (人数 / 目の色) だけを、全 scene 分
# 1 画面で埋めるための道具。scene を 1 件ずつ開いて 3 クリックずつ、を 56 回やるのは
# 作業として重すぎる (2026-07-29 の「大変すぎる」への具体的な対処)。
# **人数は人物カードから導いた既定値を出す** — 新しい判断は要らないので確認だけ。
# 目の色は見ないと決まらないので空で出す。
FILL_PAGE = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>kura1 補完 — presentation</title>
<style>
:root{--bg:#14161a;--panel:#1b1e24;--line:#2b3038;--fg:#dfe3ea;--dim:#8b94a3;
      --acc:#5fd68a;--warn:#f0a050;--err:#f07a7a;--sel:#7aa8f0}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}
header{position:sticky;top:0;z-index:10;display:flex;gap:14px;align-items:center;
  padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line)}
h1{font-size:15px;margin:0}
a{color:var(--sel)}
button{font:inherit;color:var(--fg);background:#1a2230;border:1px solid var(--sel);
  border-radius:4px;padding:5px 12px;cursor:pointer}
button.go{background:#16301f;border-color:var(--acc);color:var(--acc);font-weight:600}
.row{display:grid;grid-template-columns:132px 90px 1fr;gap:14px;padding:12px 16px;
  border-bottom:1px solid var(--line);align-items:start}
.row.done{opacity:.45}
img{width:132px;height:132px;object-fit:contain;background:#0d0f12;border:1px solid var(--line);
  border-radius:4px;cursor:zoom-in}
img.big{position:fixed;inset:4vh 4vw;width:auto;height:92vh;z-index:100;object-fit:contain;
  background:#000e;cursor:zoom-out}
.who{font-weight:700;font-size:13px}
.sub{display:flex;gap:8px;align-items:center;margin:3px 0;flex-wrap:wrap}
.dot{width:10px;height:10px;border-radius:50%;box-shadow:0 0 0 1px #000c;flex:0 0 auto}
select,input{font:inherit;color:var(--fg);background:#0f1216;border:1px solid var(--line);
  border-radius:4px;padding:3px 6px}
.cnt{color:var(--dim);font-size:12px}
.hint{color:var(--dim);font-size:12px}
.st{font-size:12px;padding:1px 7px;border-radius:999px;border:1px solid var(--line);color:var(--dim)}
.st.ok{border-color:var(--acc);color:var(--acc)}
.st.err{border-color:var(--err);color:var(--err)}
</style></head><body>
<header><h1>kura1 補完</h1>
  <span class="hint">人物ごとの <b>presentation</b> だけを埋めます。
    <b>人数はここから数える</b>ので別に入力しません (entity_count は「列挙できない群衆」専用)。</span>
  <span style="flex:1"></span><span id="prog" class="hint"></span>
  <button class="go" onclick="saveAll()">全部まとめて確定</button>
  <a href="/">← 注釈へ戻る</a>
</header>
<div id="list"></div>
<script>

// ══════════════════════════════════════════════════════════════════════════
// **API の基点をページの位置から導く。** 将来 kura の 1 モードとして
// `/annotate/` のような prefix の下にマウントできるようにするための準備
// (user 指示 2026-07-30「今後 kura のモードの一つにできるように準備しておくこと」)。
// 絶対パス (`/api/vocab`) を直書きしていると prefix の下では 404 になる。
// ページが `/x/y/` から配られたら基点は `/x/y` — server 側の設定は要らない。
// 詳細な統合手順は database/ANNOTATE-UI-INTEGRATION.md
// ══════════════════════════════════════════════════════════════════════════
const API = location.pathname.replace(/\/[^/]*$/, '');
const U = path => API + path;      // U('/api/vocab') → '/annotate/api/vocab'

const $=id=>document.getElementById(id);
const el=(t,a={},...c)=>{const n=document.createElement(t);
  for(const[k,v]of Object.entries(a)){if(k=='on')Object.entries(v).forEach(([e,f])=>n.addEventListener(e,f));
  else if(k=='cls')n.className=v;else n.setAttribute(k,v)} c.flat().forEach(x=>n.append(x)); return n};
const COLORS=['#7aa8f0','#f0a050','#5fd68a','#e879c8','#f0d24a','#5fd6d6','#c08cf5','#f07a7a'];
let V={}, F=[];
async function boot(){
  V=await (await fetch(U('/api/vocab'))).json();
  F=await (await fetch(U('/api/fill'))).json();
  draw();
}
function draw(){
  const need=F.reduce((a,r)=>a+r.subjects.filter(s=>s.kind==='person').length,0);
  const set=F.reduce((a,r)=>a+r.subjects.filter(s=>s.kind==='person'&&s.presentation).length,0);
  $('prog').textContent=`presentation ${set} / ${need} 人`;
  $('list').replaceChildren(...F.map((r,i)=>{
    const img=el('img',{src:U('/img/')+r.post_id+'?w=480',loading:'lazy',title:'クリックで拡大'});
    img.addEventListener('click',()=>img.classList.toggle('big'));
    const head=el('div',{},
      el('div',{cls:'who'},'post '+r.post_id),
      el('div',{cls:'hint'},'層'+r.stratum),
      el('span',{cls:'st',id:'st'+r.post_id},''));
    const body=el('div',{});
    // presentation (人物ごと)。**人数はこの数から導く**ので人数欄は置かない。
    r.subjects.forEach((s,si)=>{
      if(s.kind!=='person'){
        body.append(el('div',{cls:'sub'},
          el('span',{cls:'dot',style:'background:'+COLORS[si%COLORS.length]}),
          el('span',{},s.ref), el('span',{cls:'hint'},'('+s.kind+' — presentation は付かない)')));
        return;
      }
      const sel=el('select',{});
      sel.append(el('option',{value:''},'— 未設定 —'));
      (V.presentation||[]).forEach(v=>{const o=el('option',{value:v},v);
        if(s.presentation===v)o.selected=true; sel.append(o)});
      sel.onchange=()=>{s.presentation=sel.value; draw()};
      body.append(el('div',{cls:'sub'},
        el('span',{cls:'dot',style:'background:'+COLORS[si%COLORS.length]}),
        el('span',{},s.ref), sel));
    });
    // 導出された人数を**確認用に表示するだけ** (入力欄ではない)
    const d={};
    r.subjects.forEach(s=>{if(s.kind==='person'&&s.presentation)d[s.presentation]=(d[s.presentation]||0)+1});
    const ks=Object.keys(d).sort();
    body.append(el('div',{cls:'hint'}, ks.length
      ? '→ 人数: '+ks.map(k=>k+' '+d[k]).join(' / ')+' (人物カードから導出)'
      : '→ presentation が未設定なので人数を導けません'));
    return el('div',{cls:'row'}, img, head, body);
  }));
}
async function saveAll(){
  let ok=0, ng=0;
  for(const r of F){
    const st=$('st'+r.post_id);
    st.textContent='送信中'; st.className='st';
    try{
      const res=await (await fetch(U('/api/fill_commit'),{method:'POST',
        body:JSON.stringify({post_id:r.post_id,
          presentations:r.subjects.filter(s=>s.kind==='person'&&s.presentation)
                                  .map(s=>({ref:s.ref,value:s.presentation}))})})).json();
      if(res.errors&&res.errors.length){ st.textContent=res.errors[0].slice(0,40); st.className='st err'; ng++ }
      else { st.textContent='確定 +'+res.written.observation; st.className='st ok'; ok++ }
    }catch(e){ st.textContent='失敗'; st.className='st err'; ng++ }
  }
  $('prog').textContent=`確定 ${ok} / 失敗 ${ng}`;
}
boot();
</script></body></html>
"""


class V6Server(ThreadingHTTPServer):
    address_family = __import__("socket").AF_INET6


# ── pool オブザベーションビュー ──────────────────────────────────────────
# 「片方の画面で画像を確認しながらもう片方で印を付けるのは不合理」(user 2026-08-01)。
# pool (一括判定の器) のサムネイルと人物×属性のワンクリック付与を**同一画面**に置く。
# ?attr=&region= で対象属性を変えられる (既定は紅潮帰属: flush_intensity @face)。
POOL_PAGE = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pool __POOL_ID__ — observation view</title>
<style>
:root{--bg:#14161b;--fg:#d8dce4;--dim:#8a919e;--acc:#5fd68a;--warn:#d0812f;--err:#e05f5f}
body{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif;margin:0;padding:14px}
h1{font-size:16px;margin:0 0 10px}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.card{background:#1b1e25;border:1px solid #2a2f3a;border-radius:8px;padding:8px}
.card img{width:100%;border-radius:6px;cursor:zoom-in}
.pid{font-size:12px;color:var(--dim);margin:4px 0}
.pid a{color:#7fb2e5}
.refrow{display:flex;flex-wrap:wrap;gap:5px;align-items:center;margin:4px 0}
.refname{font-weight:700;font-size:12px;min-width:70px}
.vbtn{font-size:12px;padding:2px 8px;border-radius:5px;border:1px solid #3a4150;
  background:#232733;color:var(--fg);cursor:pointer}
.vbtn.cur{border-color:var(--acc);color:var(--acc);font-weight:700}
.vbtn:hover{background:#2c3140}
.mhint{font-size:10px;color:var(--warn);border:1px solid rgba(208,129,47,.5);
  border-radius:4px;padding:0 5px;cursor:help}
#stat{position:fixed;right:14px;bottom:12px;background:#232733;border:1px solid #3a4150;
  border-radius:8px;padding:6px 12px;display:none}
.done{outline:2px solid rgba(95,214,138,.35)}
/* anchor モード: 画像上の点 (緑=human 実値、橙枠=モデルのヒント) */
.imgwrap{position:relative;display:block}
.imgwrap.armed img{cursor:crosshair}
.dot{position:absolute;width:12px;height:12px;border-radius:50%;
  transform:translate(-50%,-50%);pointer-events:none;font-size:8px;line-height:12px;
  text-align:center;color:#08130c;font-weight:700}
.dot.hu{background:var(--acc);box-shadow:0 0 4px #000}
.dot.mo{border:2px solid var(--warn);background:rgba(0,0,0,.25);color:var(--warn)}
.vbtn.armed{border-color:#7fb2e5;color:#7fb2e5;font-weight:700}
.vbtn.has{border-color:var(--acc);color:var(--acc)}
</style></head><body>
<h1 id="title">pool __POOL_ID__</h1>
<div class="pid" id="sub"></div>
<div id="grid">読み込み中…</div>
<div id="stat"></div>
<script>
const POOL=__POOL_ID__;
const q=new URLSearchParams(location.search);
const ATTR=q.get('attr')||'flush_intensity', REGION=q.get('region')||'face';
function stat(m,c){const s=document.getElementById('stat');
  s.textContent=m;s.style.display='block';s.style.color=c||'var(--fg)';
  clearTimeout(stat._t);stat._t=setTimeout(()=>s.style.display='none',2500)}
async function setVal(post,ref,val,btn){
  try{
    const r=await(await fetch('/api/quick_attr',{method:'POST',
      body:JSON.stringify({post,ref,attribute:ATTR,value:val,region:REGION})})).json();
    if(!r.ok){stat('NG: '+(r.error||''),'var(--err)');return}
    // 同じ行の選択状態を付け替える
    const row=btn.closest('.refrow');
    row.querySelectorAll('.vbtn').forEach(b=>b.classList.toggle('cur',b===btn));
    btn.closest('.card').classList.add('done');
    stat(`${post} ${ref}: ${ATTR}=${val} を記録`,'var(--acc)');
  }catch(e){stat('通信失敗','var(--err)')}
}
// ── anchor モード: 人物ボタンで武装 → 画像クリックで頭中心を記録 ──
let AREGION='head';   // 指す部位: 頭が見えれば head、無ければ waist、それも無ければ body
async function setAnchor(post,ref,x,y,card){
  try{
    const r=await(await fetch('/api/quick_anchor',{method:'POST',
      body:JSON.stringify({post,ref,x,y,region:AREGION})})).json();
    if(!r.ok){stat('NG: '+(r.error||''),'var(--err)');return false}
    stat(`${post} ${ref}: ${AREGION} (${x.toFixed(2)}, ${y.toFixed(2)}) を記録`,'var(--acc)');
    return true;
  }catch(e){stat('通信失敗','var(--err)');return false}
}
async function anchorMode(){
  const d=await(await fetch(`/api/pool_anchor/${POOL}`)).json();
  document.getElementById('title').textContent=`${d.name} — 位置付与`;
  const sub=document.getElementById('sub');
  sub.textContent=`${d.posts.length} 枚。指す部位: `;
  [['head','頭'],['waist','腰'],['body','体 (部位不明)']].forEach(([v,lbl])=>{
    const b=document.createElement('button');
    b.className='vbtn'+(v===AREGION?' cur':'');b.textContent=lbl;
    b.onclick=()=>{AREGION=v;
      sub.querySelectorAll('.vbtn').forEach(x=>x.classList.toggle('cur',x===b))};
    sub.append(b);
  });
  sub.append(' — 人物ボタン → 画像クリック。頭 = 髪を含む頭全体の重心 (鼻や眉間ではない)。'
    +'緑点 = 記録済み、橙丸 = モデルのヒント。'
    +'付与済みの人物をもう一度クリックすると付け直し (旧行は自動撤回)。');
  const g=document.getElementById('grid');g.replaceChildren();
  d.posts.forEach(p=>{
    const card=document.createElement('div');card.className='card';
    const wrap=document.createElement('div');wrap.className='imgwrap';
    const img=document.createElement('img');
    img.src=`/img/${p.post}?w=480`;img.loading='lazy';
    wrap.append(img);card.append(wrap);
    const pid=document.createElement('div');pid.className='pid';
    pid.innerHTML=`<a href="/?post=${p.post}" target="_blank">${p.post}</a>`;
    card.append(pid);
    const row=document.createElement('div');row.className='refrow';
    let armed=null;
    const dots={};
    const putDot=(ref,pt,cls,label)=>{
      if(dots[ref+cls]) dots[ref+cls].remove();
      const dt=document.createElement('span');dt.className='dot '+cls;
      dt.style.left=(pt.x*100)+'%';dt.style.top=(pt.y*100)+'%';
      dt.textContent=label||'';
      wrap.append(dt);dots[ref+cls]=dt;
    };
    const btns={};
    const allDone=()=>p.refs.every(rf=>rf.human);
    const arm=(ref)=>{armed=ref;
      Object.entries(btns).forEach(([r,b])=>b.classList.toggle('armed',r===ref));
      wrap.classList.toggle('armed',!!ref)};
    p.refs.forEach((rf,i)=>{
      const b=document.createElement('button');
      b.className='vbtn'+(rf.human?' has':'');
      b.textContent=rf.ref;btns[rf.ref]=b;
      b.onclick=()=>arm(armed===rf.ref?null:rf.ref);
      row.append(b);
      if(rf.human) putDot(rf.ref,rf.human,'hu',String(i+1));
      if(rf.model) putDot(rf.ref,rf.model,'mo',String(i+1));
    });
    card.append(row);
    if(allDone()) card.classList.add('done');
    img.onclick=async e=>{
      if(!armed){window.open(`/img/${p.post}`,'_blank');return}
      const rc=img.getBoundingClientRect();
      const x=(e.clientX-rc.left)/rc.width, y=(e.clientY-rc.top)/rc.height;
      const ref=armed;
      if(await setAnchor(p.post,ref,x,y,card)){
        const i=p.refs.findIndex(r2=>r2.ref===ref);
        p.refs[i].human={x,y};
        putDot(ref,{x,y},'hu',String(i+1));
        btns[ref].classList.add('has');
        // 次の未付与人物へ自動武装 (流れるように連続クリックできる)
        const nxt=p.refs.find(r2=>!r2.human);
        arm(nxt?nxt.ref:null);
        if(allDone()) card.classList.add('done');
      }
    };
    g.append(card);
  });
}
async function attrMode(){
  const d=await(await fetch(`/api/pool_obs/${POOL}?attr=${ATTR}&region=${REGION}`)).json();
  document.getElementById('title').textContent=`${d.name} — ${ATTR}${REGION?' @'+REGION:''}`;
  document.getElementById('sub').textContent=
    `${d.posts.length} 枚。ボタン 1 クリックで human 名義の観測を記録 (橙 = モデルの提案、参考)。`
    +`画像クリックで原寸、post 番号クリックで注釈 UI。`;
  const g=document.getElementById('grid');g.replaceChildren();
  d.posts.forEach(p=>{
    const card=document.createElement('div');card.className='card';
    const img=document.createElement('img');
    img.src=`/img/${p.post}?w=480`;img.loading='lazy';
    img.onclick=()=>window.open(`/img/${p.post}`,'_blank');
    card.append(img);
    const pid=document.createElement('div');pid.className='pid';
    pid.innerHTML=`<a href="/?post=${p.post}" target="_blank">${p.post}</a>`
      +(p.refs.length?'':' <span style="color:var(--err)">kura1 未import</span>');
    card.append(pid);
    p.refs.forEach(rf=>{
      const row=document.createElement('div');row.className='refrow';
      const nm=document.createElement('span');nm.className='refname';nm.textContent=rf.ref;
      row.append(nm);
      d.values.forEach(v=>{
        const b=document.createElement('button');b.className='vbtn'+(rf.human===v?' cur':'');
        b.textContent=v;b.onclick=()=>setVal(p.post,rf.ref,v,b);
        row.append(b);
      });
      if(rf.model){const m=document.createElement('span');m.className='mhint';
        m.textContent='モ '+rf.model;m.title='モデルの提案 (最新世代)。クリックはあなたの判断で';
        row.append(m)}
      if(rf.human) card.classList.add('done');
      card.append(row);
    });
    g.append(card);
  });
}
if(q.get('mode')==='anchor'){anchorMode()}else{attrMode()}
</script></body></html>"""


def main() -> int:
    ANN_DIR.mkdir(parents=True, exist_ok=True)
    # **tailnet だけに公開する。** この UI は認証なしで kura1 に書けるので、
    # 0.0.0.0 に bind すると LAN からも書けてしまう。primary の Tailscale IPv6 に
    # 限定すれば tailnet 内からのみ到達可能 (CLAUDE.md: ノード指定は IPv6 native)。
    # アドレスは unit の KURA1_ANNOTATE_HOST が持つ。未設定なら loopback (::1) だけに出す。
    host = __import__("os").environ.get("KURA1_ANNOTATE_HOST", "::1")
    srv = V6Server((host, PORT), Handler)
    print(f"kura1 annotate UI → http://[{host}]:{PORT}/  (worklist {len(worklist())} 件)")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
