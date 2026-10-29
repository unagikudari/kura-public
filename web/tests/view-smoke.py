#!/usr/bin/env python
"""view-smoke.py — kura web UI の golden スクリーンショット照合。

plan slug:kura-web-componentization Step 2。tmp/verify-view-smoke/round*.py
系譜の institutionalize。fixture (fixtures.py, 変異禁止) を被写体に、主要
surface を固定条件 (viewport 1400×1000 / reduced-motion / アニメ停止 /
fonts.ready / 可視 img settle) で撮影し、web/tests/goldens/ の golden と
pixel 単位で比較する。後続 extraction phase (P2 TagChip〜) の
「screenshot diff = 0」acceptance rail。

usage (kindle venv の playwright で実行):
    ~/kindle-capture-venv/bin/python web/tests/view-smoke.py                # 照合
    ~/kindle-capture-venv/bin/python web/tests/view-smoke.py --update-golden
    ~/kindle-capture-venv/bin/python web/tests/view-smoke.py --surface grid,tags

golden 再撮影の作法 (plan Step 2): 正当な UI 変更が landed したときのみ
--update-golden → golden の画像 diff を人間レビュー → commit。
黙って golden を貼り替えて green にするのは規約違反。

照合結果: 全 surface diff=0 → exit 0 / どれかに差分 → exit 1 (差分 heatmap を
web/tests/out/ に出力、gitignored)。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image, ImageChops
from playwright.sync_api import sync_playwright

import kura_harness as H
from fixtures import (
    MANGA_LOCALSTORAGE_BASE,
    ORIGIN,
    POOL,
    POST_MANGA,
    POST_SINGLE,
    VERIFY_TARGET_TAG,
)

TESTS_DIR = Path(__file__).resolve().parent
GOLDEN_DIR = TESTS_DIR / "goldens"
OUT_DIR = TESTS_DIR / "out"

# pixel 同一性の判定閾値: チャンネル差がこの値以下は「同一」とみなす。
# swiftshader でも lazy-load 画像の decode タイミングで compositor の丸めが
# ±1 揺れる (実測 2026-07-19: 1.4M pixel 中 1〜5 px が delta=1)。実際の UI
# 変化は必ずこれを大きく超えるので、±2 は AA ジッタ吸収であって視覚差の容認
# ではない。
CHANNEL_TOLERANCE = 2

# 総差分 pixel のノイズフロア: 選択枠角などの AA が channel ±2 を超えて
# run ごとに揺れる箇所が数 px ある (実測 2026-07-20: post-single のサムネ
# ストリップ選択枠の角 4 px が ±16/ch で二状態フリッカー)。実際の UI 変化は
# 数百 px 以上になるので、総数がこのフロア以下なら同一とみなす。
PIXEL_NOISE_FLOOR = 8

# ── golden surface 定義 (plan Step 2 (e)) ──
# NOTE: /lora は LoRA plan P3 の agent が並行改修中 (2026-07-19) のため golden
# 集合から除外している。P3 が landed して surface が安定したら追加すること。
SURFACES: dict[str, dict] = {
    "grid": {
        "path": f"/?q=pool%3A{POOL}",
        "local_storage": None,
    },
    "grid-bulk": {
        "path": f"/?q=pool%3A{POOL}&bulk=1",
        "local_storage": None,
    },
    "post-single": {
        "path": f"/post/{POST_SINGLE}?q=pool%3A{POOL}",
        "local_storage": None,
    },
    "manga": {
        "path": f"/post/{POST_MANGA}?q=pool%3A{POOL}&mode=manga",
        "local_storage": dict(MANGA_LOCALSTORAGE_BASE),
    },
    "manga-bulk-overlay": {
        "path": f"/post/{POST_MANGA}?q=pool%3A{POOL}&mode=manga",
        "local_storage": {
            **MANGA_LOCALSTORAGE_BASE,
            "kura_verify_header": "1",
            "kura_verify_overlay": "1",
            "kura_verify_target": VERIFY_TARGET_TAG,
        },
    },
    "tags": {
        "path": "/tags",
        "local_storage": None,
        # 右ペインは既定ギャラリー (/?q=&embed=1) の iframe 埋め込み = 最新
        # 投稿の live データで、取り込みが走るたび変わる。golden はサイドバー+
        # ヘッダーを対象にし、iframe は Playwright mask (一様なマゼンタ矩形)
        # で決定論化する。
        "mask": ["iframe.gallery"],
    },
}


def capture(names: list[str], dest: Path) -> dict[str, Path]:
    dest.mkdir(parents=True, exist_ok=True)
    shots: dict[str, Path] = {}
    with sync_playwright() as p:
        browser = H.launch_browser(p)
        ctx = H.new_context(browser)
        page = ctx.new_page()
        for name in names:
            spec = SURFACES[name]
            H.goto_settled(page, ORIGIN + spec["path"], local_storage=spec["local_storage"])
            path = dest / f"{name}.png"
            mask = [page.locator(sel) for sel in spec.get("mask", [])]
            page.screenshot(path=str(path), mask=mask)
            shots[name] = path
            print(f"  captured {name}")
        browser.close()
    return shots


def pixel_diff(a: Path, b: Path) -> tuple[int, Image.Image | None]:
    """(CHANNEL_TOLERANCE 超の差分 pixel 数, 差分 heatmap)。
    サイズ不一致は全 pixel 差分扱い。"""
    ia, ib = Image.open(a).convert("RGB"), Image.open(b).convert("RGB")
    if ia.size != ib.size:
        return ia.size[0] * ia.size[1], None
    diff = ImageChops.difference(ia, ib)
    # 閾値以下のチャンネル差を 0 に潰してから数える
    thresh = diff.point(lambda v: 0 if v <= CHANNEL_TOLERANCE else 255)
    if thresh.getbbox() is None:
        return 0, None
    px = thresh.load()
    w, h = thresh.size
    nonzero = sum(1 for y in range(h) for x in range(w) if px[x, y] != (0, 0, 0))
    return nonzero, diff


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--update-golden", action="store_true",
                    help="golden を現ツリーの描画で更新する (人間レビュー必須)")
    ap.add_argument("--surface", default=None,
                    help="カンマ区切りで surface を限定 (default: 全部)")
    args = ap.parse_args()

    names = list(SURFACES) if not args.surface else [
        s.strip() for s in args.surface.split(",")]
    for n in names:
        if n not in SURFACES:
            ap.error(f"unknown surface {n!r} (known: {', '.join(SURFACES)})")

    if args.update_golden:
        print(f"updating goldens: {', '.join(names)}")
        capture(names, GOLDEN_DIR)
        print(f"goldens written to {GOLDEN_DIR} — diff を人間レビューして commit すること")
        return 0

    print(f"comparing surfaces: {', '.join(names)}")
    shots = capture(names, OUT_DIR)
    failures = []
    for name in names:
        golden = GOLDEN_DIR / f"{name}.png"
        if not golden.exists():
            print(f"FAIL {name}: golden missing ({golden}) — run --update-golden first")
            failures.append(name)
            continue
        n, diff = pixel_diff(golden, shots[name])
        if n == 0:
            print(f"OK   {name}: pixel diff 0")
        elif n <= PIXEL_NOISE_FLOOR:
            print(f"OK   {name}: pixel diff {n} (≤ noise floor {PIXEL_NOISE_FLOOR})")
        else:
            heat = OUT_DIR / f"{name}.diff.png"
            if diff is not None:
                diff.save(heat)
            print(f"FAIL {name}: {n} pixels differ (heatmap: {heat})")
            failures.append(name)
    if failures:
        print(f"\nFAIL: {len(failures)}/{len(names)} surfaces differ: {', '.join(failures)}")
        return 1
    print(f"\nALL PASS: {len(names)} surfaces, pixel diff 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
