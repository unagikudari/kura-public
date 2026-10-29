#!/usr/bin/env python3
"""dupe-audit.py — kura web UI 重複台帳 (duplication ledger) の生成と検査。

plan slug:kura-web-componentization Step 3 (P1, finding 6+8)。
web/src を candidate ごとの検出 regex で走査し、`web/tests/dupe-ledger.json`
を生成する。schema (per candidate):

  {candidate, sites: [{file, line}], lines, surface, state_coupling, rank, status}

  - sites   … pattern が (site_min_matches 行以上) 当たったファイルと初出行
  - lines   … pattern 一致行の総数 = 重複実装量の再現可能な下界 proxy
              (ブロック全体の行数は主観が入るので、一致行数で統一測定する)
  - surface … 影響する user-facing surface 数 (定義に宣言、根拠は notes)
  - state_coupling … low|med|high (定義に宣言)
  - rank    = len(sites) × lines × surface × coupling_weight
              (low=1 / med=2 / high=3 — 抽出優先度。ただし plan の決定事項
               どおり high coupling は rank に関わらず deferred/分割候補)
  - status  … open|extracted|rejected|deferred。再実行は測定値だけ更新し
              status と resolution note は既存 ledger から引き継ぐ。

usage:
  dupe-audit.py            # 台帳を(再)生成して ranked summary を表示
  dupe-audit.py --check    # 完遂条件検査: 全 candidate が resolved
                           #   (extracted|rejected|deferred) なら exit 0、
                           #   open が残っていれば exit 1
                           # (plan: 各 phase の開始と終了に実行する)

status の更新は台帳 JSON を直接編集して commit する (resolution の判断は
人間/agent の仕事、測定は機械の仕事という分担)。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WEB_SRC = REPO / "web" / "src"
LEDGER = REPO / "web" / "tests" / "dupe-ledger.json"

COUPLING_WEIGHT = {"low": 1, "med": 2, "high": 3}
RESOLVED = {"extracted", "rejected", "deferred"}

# ── candidate 定義 ──
# pattern: 検出 regex (行単位)。glob: 走査対象。site_min_matches: この行数
# 以上当たったファイルだけを site と数える (かすり傷の誤検出除け)。
# surface / state_coupling は宣言値 — 根拠を notes に書く。
CANDIDATES: list[dict] = [
    {
        "candidate": "tag-suggestion-dropdown",
        # P3 抽出後に pattern を精密化 (2026-07-19、P2 tag-chip-render と同じ流儀):
        # 旧 r"(?i)sug" は共有コンポーネントの消費配線 (onPick={(sug)=>…} /
        # applySuggestion / コメント) まで数えてしまう。dropdown を「実装している」
        # ことの判別子は suggestion fetch の呼び出し (searchTags()) — 抽出前は
        # 全 6 site が各自 fetch していた (契約表 p3-dropdown-contracts.json 参照)。
        "pattern": r"searchTags\(",
        "glob": ["**/*.svelte"],
        "site_min_matches": 1,
        "surface": 6,
        "state_coupling": "med",
        "notes": "searchTags() fetch は api.ts で共有済みだが UI/state (debounce, "
                 "highlight, keyboard nav, dropdown CSS) が site ごとに再実装だった。"
                 "surface=6: Header(全ページ)/BulkTagBar(グリッド+マンガ)/Lightbox/"
                 "SinglePostView/post-detail/tags-edit。Header は token.includes(':') "
                 "filter など divergent 意味論あり → P3 で contract table を先に作った。",
    },
    {
        "candidate": "tag-chip-render",
        # P2 抽出後に pattern を精密化 (2026-07-19): 旧 r"(?:spv|lb|vto)-tag|class=\"tag\""
        # は chip 周辺のレイアウト class (spv-tag-list / lb-tagbar / lb-tag-input 等、
        # chip ではない) にも当たり続けるため、chip class の exact 一致 + 抽出先
        # TagChip.svelte の class (.tc-* / "tag-chip") を検出するよう変更。
        # 検出意図 (chip 描画の実装がどこにあるか) は不変。
        "pattern": r"(?:spv|lb|vto)-tag(?![a-zA-Z-])|class=\"tag\"|\.tc-|\"tag-chip",
        "glob": ["**/*.svelte"],
        "site_min_matches": 2,
        "surface": 4,
        "state_coupling": "low",
        "notes": "タグ chip (名前+catColor 枠+optional remove ×) の描画ファミリが "
                 "spv-tag / lb-tag / vto-tag / tags ブラウザ .tag で並立していた。"
                 "P2 で lib/components/TagChip.svelte (variant: single/lightbox/"
                 "overlay/pill) へ抽出 — 旧 4 site のインライン markup/styles は削除済み。",
    },
    {
        "candidate": "category-color-source",
        "pattern": r"catColor|CAT_COLOR|categoryColor",
        "glob": ["**/*.svelte", "**/*.ts"],
        "exclude": ["lib/categoryColor.ts"],  # 定義ファイル自身は site ではない
        "site_min_matches": 1,
        "surface": 6,
        "state_coupling": "med",
        "notes": "hardcoded DB-hue-derived map (categoryColor.ts) と server-driven "
                 "TagDetail.categoryColor (/tags 系のみ) の二重 SoT。P4 で DB 駆動へ "
                 "一本化 (map は fallback に降格)。coupling med は SSR 供給路の追加込み。",
    },
    {
        "candidate": "keyboard-shortcut-registration",
        "pattern": r"<svelte:window[^>]*onkeydown|\be\.key\s*===",
        "glob": ["**/*.svelte"],
        "site_min_matches": 2,
        "surface": 3,
        "state_coupling": "high",
        "notes": "グローバル key map が post/[id] / SinglePostView / Lightbox で "
                 "各自 svelte:window onkeydown 登録 (+ 各 input の局所 handler)。"
                 "重なり合う key (arrows/e/t/x/Space) の優先順位が暗黙 → coupling high、"
                 "plan の決定事項どおり rank に関わらず deferred/分割抽出候補。",
    },
    {
        "candidate": "flash-toast-pattern",
        # P5 抽出後に pattern を精密化 (2026-07-20、P2/P3 と同じ流儀): 旧
        # r"(?i)\bflash|uploadMsg|toast" は共有コンポーネントの消費配線
        # (flash= prop / コメント) まで数える。flash/toast を「実装している」
        # ことの判別子は表示シェルの class 群 — 抽出前は VerifyTagOverlay
        # (vto-flash/vto-undo) と +layout (upload-toast/toast-link) が各自
        # markup+CSS を持っていた。auto-dismiss タイマーは lib/flash.svelte.ts
        # createFlash が単一実装 (呼び出し側の setTimeout 手書きは全廃)。
        "pattern": r"vto-flash|vto-undo|upload-toast|toast-link",
        "glob": ["**/*.svelte"],
        "site_min_matches": 2,
        "surface": 3,
        "state_coupling": "med",
        "notes": "一時通知 (flash/undo/upload msg) が post/[id] / VerifyTagOverlay / "
                 "+layout (upload) で setTimeout 手書き実装だった。P5 で表示シェルを "
                 "FlashMessage.svelte (variant: overlay/toast) に、消滅タイマーを "
                 "lib/flash.svelte.ts createFlash に抽出 — ドメイン状態 (何をいつ出すか) "
                 "は呼び出し側のまま (plan の決定事項どおり state は caller-side)。",
    },
    {
        "candidate": "pool-verify-badge",
        "pattern": r"badge-verify|pool-verify-chip|検証済|検証待",
        "glob": ["**/*.svelte"],
        "site_min_matches": 1,
        "surface": 2,
        "state_coupling": "low",
        "notes": "pool の検証状態表示がグリッド pool カード (済/検 badge) と "
                 "PoolTitleBar (cycle chip) で別実装だった。意味論 (pending/verified/"
                 "excluded/null) は共通。P5 で PoolVerifyBadge.svelte (variant: "
                 "card/chip) へ抽出 — card は verify > domain > 名前 heuristic の "
                 "badge 優先順位分岐ごと単一化、状態 cycle の PATCH は親のまま。",
    },
    {
        "candidate": "hide-verify-logic",
        # P5 抽出後に pattern を精密化 (2026-07-20): 旧 r"hideVerify|hide_verify|
        # kura_hideverify" は boolean prop の伝搬 (Header consumer) や API 層の
        # 単一 query param (api.ts fetchPools) まで数える。ロジックを「実装して
        # いる」ことの判別子は状態 SoT である cookie 名 kura_hideverify の操作 —
        # 抽出前は routes/+page.svelte が読み書きをインライン実装していた。
        "pattern": r"kura_hideverify",
        "glob": ["**/*.svelte", "**/*.ts"],
        "site_min_matches": 1,
        "surface": 2,
        "state_coupling": "med",
        "notes": "検証 pool 非表示が cookie 読み書き (grid) + UI toggle (Header 2 箇所) "
                 "+ api param と分散、状態の SoT が cookie 文字列比較に埋まっていた。"
                 "P5 で cookie 読み書きを lib/hideVerify.ts (readHideVerify/"
                 "writeHideVerify) へ一本化 — Header は props 消費、api.ts の "
                 "hide_verify param は API 層の単一片でどちらも複製ではない。",
    },
    {
        "candidate": "inline-fetch-vs-api",
        "pattern": r"\bfetch\(",
        "glob": ["routes/**/*.svelte", "lib/components/**/*.svelte"],
        "site_min_matches": 1,
        "surface": 1,
        "state_coupling": "low",
        "notes": "api.ts を迂回する生 fetch() の残存。現ツリーでは status ページのみ "
                 "(Phase 0 までの掃除で他は解消済み) — 低 rank、reject 候補。",
    },
]


def measure(c: dict) -> dict:
    pat = re.compile(c["pattern"])
    excludes = c.get("exclude", [])
    sites: list[dict] = []
    total_lines = 0
    files: set[Path] = set()
    for g in c["glob"]:
        files.update(WEB_SRC.glob(g))
    for f in sorted(files):
        rel = f.relative_to(WEB_SRC).as_posix()
        if any(rel == e or rel.startswith(e.rstrip("/") + "/") for e in excludes):
            continue
        try:
            text = f.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        hits = [i + 1 for i, line in enumerate(text.splitlines()) if pat.search(line)]
        if len(hits) >= c["site_min_matches"]:
            sites.append({"file": "web/src/" + rel, "line": hits[0], "matches": len(hits)})
            total_lines += len(hits)
    weight = COUPLING_WEIGHT[c["state_coupling"]]
    return {
        "candidate": c["candidate"],
        "sites": sites,
        "lines": total_lines,
        "surface": c["surface"],
        "state_coupling": c["state_coupling"],
        "rank": len(sites) * total_lines * c["surface"] * weight,
        "status": "open",
        "notes": c["notes"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="完遂条件検査のみ (open が残っていれば exit 1)")
    args = ap.parse_args()

    if args.check:
        if not LEDGER.exists():
            print(f"FAIL: ledger missing: {LEDGER}", file=sys.stderr)
            return 2
        ledger = json.loads(LEDGER.read_text())
        unresolved = [c["candidate"] for c in ledger["candidates"]
                      if c.get("status") not in RESOLVED]
        if unresolved:
            print("FAIL: unresolved candidates (need extracted|rejected|deferred):")
            for name in unresolved:
                print(f"  - {name}")
            return 1
        print(f"OK: all {len(ledger['candidates'])} candidates resolved")
        return 0

    # 再生成: 測定値は現ツリーから、status/resolution は既存 ledger から引き継ぐ
    previous: dict[str, dict] = {}
    if LEDGER.exists():
        try:
            previous = {c["candidate"]: c
                        for c in json.loads(LEDGER.read_text())["candidates"]}
        except (json.JSONDecodeError, KeyError):
            print("WARN: existing ledger unparseable — starting fresh", file=sys.stderr)

    rows = []
    for c in CANDIDATES:
        row = measure(c)
        prev = previous.get(row["candidate"])
        if prev:
            row["status"] = prev.get("status", "open")
            if prev.get("resolution"):
                row["resolution"] = prev["resolution"]
        rows.append(row)
    rows.sort(key=lambda r: -r["rank"])

    out = {
        "_meta": {
            "description": "kura web UI duplication ledger — plan "
            "kura-web-componentization P1. Regenerate via "
            "scripts/devtools/dupe-audit.py (measurements are machine-derived; "
            "status/resolution are hand-edited and preserved across re-runs). "
            "rank = sites × lines × surface × coupling_weight.",
            "coupling_weight": COUPLING_WEIGHT,
            "lines_semantics": "pattern-matched line count per candidate — "
            "reproducible lower-bound proxy for duplicated implementation size",
        },
        "candidates": rows,
    }
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {LEDGER}\n")
    print(f"{'rank':>6}  {'candidate':<32} {'sites':>5} {'lines':>5} {'surf':>4} "
          f"{'coupling':<8} status")
    for r in rows:
        print(f"{r['rank']:>6}  {r['candidate']:<32} {len(r['sites']):>5} "
              f"{r['lines']:>5} {r['surface']:>4} {r['state_coupling']:<8} {r['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
