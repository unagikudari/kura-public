#!/usr/bin/env python3
"""人物の位置を機械が提案する (S-8 の補助。**確定ではなく提案**)。

plan kura-1.0-implementation の S-8 中の指摘、2026-07-28:
「あらかじめ機械側が算出したものをこちらで多少四隅をドラッグして補正するか、
  中心位置を取るとか。例えば頭の中心とか」。

── 何を出すか ──
`face_yolov8m` (アニメ顔検出) で顔を取り、その中心を **head_center の候補**として返す。
`person_yolov8m-seg` (人物) の box も返すが、絡み合った人物では box がほぼ同一になり
主体を同定できないので **顔が主、person は補助**。

── 提案であって観測ではない ──
返り値は注釈 UI の下書きに入るだけで、DB には書かない。人が見て、要らないものを消し、
ずれているものを動かして初めて観測になる。**検出器が出したというだけでは事実ではない** —
非捏造の原則は機械の出力にも同じように効く。だから confidence をそのまま添えて返し、
UI 側で「提案」と分かる形で見せる。

モデルは ComfyUI (Impact Pack 規約) の配置をそのまま使う — 二重に置かない。
実行は専用 venv `~/kura1-detect-venv` (ComfyUI の .venv を汚さない)。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

os.environ.setdefault("YOLO_CONFIG_DIR", "/tmp/ultralytics-kura1")

FACE_MODEL = pathlib.Path(
    str(pathlib.Path.home() / "comfyui-gfx1151/ComfyUI/models/ultralytics/bbox/face_yolov8m.pt"))
PERSON_MODEL = pathlib.Path(
    str(pathlib.Path.home() / "comfyui-gfx1151/ComfyUI/models/ultralytics/segm/person_yolov8m-seg.pt"))

CONF_FACE = 0.35      # これ未満は出さない。取りこぼしより誤検出の方が害が大きい
                      # (人が消す手間 > 人が足す手間、ではなく **間違いに引きずられる**)
CONF_PERSON = 0.40
IOU_DEDUP = 0.55      # 同じ顔を二重に出さない
BBOX_MAX_AREA = 0.75  # これ以上の面積を占める box は提案しない (情報量が無い)

_models: dict = {}


def _model(path: pathlib.Path):
    if path not in _models:
        from ultralytics import YOLO
        _models[path] = YOLO(str(path))
    return _models[path]


def _iou(a, b) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def _detect(path: pathlib.Path, model_path: pathlib.Path, conf: float) -> list[dict]:
    r = _model(model_path).predict(str(path), conf=conf, verbose=False)[0]
    h, w = r.orig_shape
    out = []
    for b, c in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist()):
        box = [b[0] / w, b[1] / h, b[2] / w, b[3] / h]
        if any(_iou(box, o["box"]) >= IOU_DEDUP for o in out):
            continue
        out.append({"box": [round(v, 4) for v in box], "conf": round(float(c), 3)})
    return out


def propose(path: pathlib.Path) -> dict:
    """{'faces': [...], 'persons': [...], 'subjects': [...]} を返す。

    subjects は「そのまま下書きに入れられる形」。顔ごとに 1 人とし、head_center を
    顔 box の中心に置く。**人数の決定ではない** — 顔が写っていない人物は出ないし、
    絵の中の絵 (ポスター等) の顔も出る。人が足し引きする前提。
    """
    faces = _detect(path, FACE_MODEL, CONF_FACE)
    persons = _detect(path, PERSON_MODEL, CONF_PERSON)
    # 左から右へ並べる (person_1, person_2, ... の付け方が画面と揃う方が照合しやすい)
    faces.sort(key=lambda f: (f["box"][0] + f["box"][2]) / 2)
    subjects = []
    for i, f in enumerate(faces, 1):
        x0, y0, x1, y1 = f["box"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        # 顔 box を包む person があれば、その box も提案に添える
        owner = None
        for p in persons:
            b = p["box"]
            if b[0] <= cx <= b[2] and b[1] <= cy <= b[3]:
                if owner is None or (b[2] - b[0]) * (b[3] - b[1]) < \
                        (owner[2] - owner[0]) * (owner[3] - owner[1]):
                    owner = b
        s = {"ref": f"person_{i}",
             "anchor": {"x": round(cx, 4), "y": round(cy, 4), "kind": "head_center"},
             "conf": f["conf"]}
        # **画面をほぼ覆う box は付けない。** 情報量がゼロで、あるだけで
        # 「位置を指した」ように見えてしまう (指摘そのもの: 人体はカメラの
        # ほとんどを占有している)。実測でも person 検出の box は 0.003〜0.998 だった。
        if owner and (owner[2] - owner[0]) * (owner[3] - owner[1]) < BBOX_MAX_AREA:
            s["bbox"] = owner
        subjects.append(s)
    return {"faces": faces, "persons": persons, "subjects": subjects}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", help="画像パス。省略時は kura1 の blob 全部を測る")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--eval", action="store_true",
                    help="注釈済みの下書きの人数と突き合わせて精度を出す")
    a = ap.parse_args()

    if a.eval:
        return _evaluate()
    if not a.paths:
        print("パスを指定するか --eval を使う", file=sys.stderr)
        return 2
    for p in a.paths:
        r = propose(pathlib.Path(p))
        if a.json:
            print(json.dumps({"path": p, **r}, ensure_ascii=False))
        else:
            print(f"{p}: 顔 {len(r['faces'])} / 人物 {len(r['persons'])}")
            for s in r["subjects"]:
                print(f"    {s['ref']} head_center=({s['anchor']['x']:.2f},"
                      f"{s['anchor']['y']:.2f}) conf={s['conf']}"
                      + ("  +bbox" if "bbox" in s else ""))
    return 0


def _evaluate() -> int:
    """人が付けた下書きの人数を正解として、顔検出の人数を突き合わせる。

    **これは「顔検出の人数一致率」であって位置の精度ではない。** 位置は人が見て
    直す前提なので、ここで測るのは「提案が出発点として役に立つか」だけ。
    """
    import psycopg
    ann_dir = pathlib.Path(os.environ.get(
        "KURA1_ANNOTATE_DIR", str(pathlib.Path.home() / "kura1" / "annotations")))
    with psycopg.connect("host=127.0.0.1 port=5541 dbname=kura1 user=desk") as c, c.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        cur.execute("SELECT s.external_id, b.external_ref FROM blob b"
                    " JOIN asset a ON a.blob_id=b.id JOIN asset_source s ON s.asset_id=a.id"
                    " WHERE s.source_slug='kura_post'")
        ref = dict(cur.fetchall())

    exact = total = 0
    print(f"{'post':>8} {'人手':>4} {'顔':>3} {'人物':>4}  判定")
    for f in sorted(ann_dir.glob("*.json")):
        if not f.stem.isdigit() or f.stem not in ref:
            continue
        human = len(json.loads(f.read_text()).get("subjects", []))
        r = propose(pathlib.Path(ref[f.stem]))
        nf, np_ = len(r["faces"]), len(r["persons"])
        hit = nf == human
        exact += hit
        total += 1
        print(f"{f.stem:>8} {human:>4} {nf:>3} {np_:>4}  {'一致' if hit else 'ずれ'}")
    if total:
        print(f"\n人数一致 {exact}/{total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
