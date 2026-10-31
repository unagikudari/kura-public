#!/usr/bin/env python3
"""画像そのものから決まる事実を測る (人手注釈に投げないもの)。

plan kura-1.0-implementation S-8 の指摘 (2026-07-28):
「画面の向きはアスペクト比から自動的に定まるのではないの？」→ その通り。同じ理屈で
`rendering` の色モード (greyscale / monochrome / sepia / colored / limited_palette /
spot_color) も**画素から測れる**ので、人の判断に投げるべきではない。

── 設計上の一線 ──
向き (orientation) は幅と高さの比較で **必ず** 決まる。色モードは測れるが、閾値の
どちら側かが微妙な画像がある (灰色基調にリボンだけ赤い → spot_color か colored か)。
そこで **決め切れたときだけ値を返し、決め切れなければ None を返す**。
None のときは注釈 UI が人に選ばせる。「測れないのに機械が断定する」ことをしない —
これは非捏造の原則そのもの。

`lineart` / `sketch` は「線が整理されているか」という判断なので**測らない**。
同じ rendering ファセットでも、descriptor 単位で導出可否が分かれる。

── 返す値 ──
measure_image(path) -> {
  "width","height","orientation",           # 必ず決まる
  "color_mode": str|None,                   # 決め切れたときだけ
  "color_why": str,                         # なぜそう判定したか (人が検算できるように)
  "metrics": {...},                         # 生の測定値
}
"""
from __future__ import annotations

import math
import pathlib
import sys

import numpy as np
from PIL import Image

# ── 閾値 (すべてここに集める。根拠はコメントに書く) ────────────────
THUMB = 256          # 長辺をここまで縮めてから測る (速度。色の統計は縮小に強い)
V_MIN = 24           # near-black は色相が雑音になるので除く (0..255)
SAT_ON = 0.12        # ここ以上を「色が付いている画素」とみなす。JPEG/WebP の
                     # 彩度ノイズは概ね 0.05 以下なので、真のグレースケールを
                     # 誤って colored にしない余裕を取ってある
HUE_WIN = 6.0        # 単色判定の色相窓 (±度)。**20° では肌色主体の普通のカラー絵が
                     # sepia と誤判定された** (2026-07-28 実測、58 枚中 6 枚)。
                     # 肌 (25°) と赤いマフラー (5°) は 20° 窓なら同じ束に入ってしまう。
                     # 6° まで絞ると誤判定は 0.44〜0.81 に落ち、閾値 0.95 に届かない
SEPIA_LO, SEPIA_HI = 15.0, 50.0   # セピアの色相帯 (橙〜茶)
PALETTE_MAX = 12     # 98% の画素をこれ以下の色数で覆えれば limited_palette


def _load(path: pathlib.Path) -> np.ndarray:
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((THUMB, THUMB))
        return np.asarray(im, dtype=np.float32)


def _metrics(rgb: np.ndarray) -> dict:
    mx = rgb.max(axis=2)
    mn = rgb.min(axis=2)
    chroma = mx - mn
    valid = mx >= V_MIN
    n_valid = int(valid.sum())
    if n_valid == 0:
        return {"n_valid": 0}
    sat = np.zeros_like(mx)
    np.divide(chroma, mx, out=sat, where=mx > 0)
    colored = valid & (sat >= SAT_ON)
    frac_colored = float(colored.sum()) / n_valid

    hue_conc = 0.0
    hue_mean = None
    if colored.sum() >= 16:
        r, g, b = rgb[..., 0][colored], rgb[..., 1][colored], rgb[..., 2][colored]
        c = chroma[colored]
        m = mx[colored]
        h = np.zeros_like(c)
        i_r = (m == r)
        i_g = (m == g) & ~i_r
        i_b = ~i_r & ~i_g
        with np.errstate(invalid="ignore", divide="ignore"):
            h[i_r] = (60 * ((g[i_r] - b[i_r]) / c[i_r])) % 360
            h[i_g] = (60 * ((b[i_g] - r[i_g]) / c[i_g]) + 120) % 360
            h[i_b] = (60 * ((r[i_b] - g[i_b]) / c[i_b]) + 240) % 360
        h = h[np.isfinite(h)]
        if h.size:
            # 色相は円環量なので、単純平均ではなく単位ベクトルの平均で代表値を出す
            rad = np.radians(h)
            cx, cy = float(np.cos(rad).mean()), float(np.sin(rad).mean())
            hue_mean = math.degrees(math.atan2(cy, cx)) % 360
            d = np.abs((h - hue_mean + 180) % 360 - 180)
            hue_conc = float((d <= HUE_WIN).mean())

    return {"n_valid": n_valid, "frac_colored": round(frac_colored, 4),
            "hue_mean": None if hue_mean is None else round(hue_mean, 1),
            "hue_conc": round(hue_conc, 3)}


def _n_colors_98(path: pathlib.Path) -> int:
    """98% の画素を覆うのに要る色数。limited_palette の判定用。

    **縮小は NEAREST でなければならない。** 既定の補間で縮めると隣接色が混ざって
    中間色が生まれ、8 色で描かれた絵が 60 色に化ける (2026-07-28 実測: 8 色に量子化した
    陽性対照が limited_palette と判定されなかった原因)。
    """
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        if max(w, h) > THUMB:
            k = THUMB / max(w, h)
            im = im.resize((max(1, int(w * k)), max(1, int(h * k))), Image.Resampling.NEAREST)
        q = im.quantize(colors=64, method=Image.Quantize.MEDIANCUT)
    hist = sorted((c for c in q.histogram() if c), reverse=True)
    total = sum(hist)
    acc = 0
    for i, c in enumerate(hist, 1):
        acc += c
        if acc >= total * 0.98:
            return i
    return len(hist)


def color_mode(path: pathlib.Path) -> tuple[str | None, str, dict]:
    """色モードを測る。**決め切れなければ (None, 理由, 測定値) を返す。**"""
    rgb = _load(path)
    m = _metrics(rgb)
    if not m.get("n_valid"):
        return None, "有効画素が無い (ほぼ真っ黒)", m
    fc, hc, hm = m["frac_colored"], m["hue_conc"], m["hue_mean"]

    if fc <= 0.01:
        return "greyscale", f"色の付いた画素が {fc:.1%} しかない", m
    # spot_color は**空間的に色が偏っている**ことであって、色相が 1 つに揃うことでは
    # ない (差し色の中に肌・赤・服の複数の色相が入りうる)。色相集中を条件にしたら
    # 陽性対照が落ちた (2026-07-28) ので、条件は「大半が無彩色」だけにする。
    if fc <= 0.10:
        return "spot_color", f"画素の {1 - fc:.0%} が無彩色で、色は {fc:.1%} だけ", m
    if hc >= 0.95:
        if hm is not None and SEPIA_LO <= hm < SEPIA_HI:
            return "sepia", f"色相が {hm:.0f}° に {hc:.0%} 集中 (橙〜茶の帯)", m
        return "monochrome", f"色相が {hm:.0f}° に {hc:.0%} 集中", m
    n = _n_colors_98(path)
    m["n_colors_98"] = n
    if n <= PALETTE_MAX:
        return "limited_palette", f"98% の画素を {n} 色で覆える", m
    if fc >= 0.25 and hc < 0.90:
        return "colored", f"色の付いた画素が {fc:.1%}、色相も散っている ({hc:.0%})", m
    return None, (f"閾値のどちら側とも言えない (色画素 {fc:.1%} / 色相集中 {hc:.0%} / "
                  f"色数 {n}) — 人が選ぶ"), m


def measure_image(path: pathlib.Path) -> dict:
    with Image.open(path) as im:
        w, h = im.size
    cm, why, met = color_mode(path)
    return {"width": w, "height": h,
            "orientation": "landscape" if w > h else ("portrait" if w < h else "square"),
            "color_mode": cm, "color_why": why, "metrics": met}


def main() -> int:
    """引数のファイル、または kura1 の blob 全部を測って分布を出す (検算用)。"""
    if len(sys.argv) > 1:
        for a in sys.argv[1:]:
            print(a, measure_image(pathlib.Path(a)))
        return 0
    import collections

    import psycopg
    with psycopg.connect("host=127.0.0.1 port=5541 dbname=kura1 user=desk") as c, c.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        cur.execute("SELECT id, external_ref FROM blob ORDER BY id")
        rows = cur.fetchall()
    cnt = collections.Counter()
    for bid, ref in rows:
        r = measure_image(pathlib.Path(ref))
        cnt[r["color_mode"]] += 1
        if r["color_mode"] != "colored":
            print(f"  blob {bid:>3} {r['color_mode'] or '判定せず':<16} {r['color_why']}")
    print("\n分布:", dict(cnt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
