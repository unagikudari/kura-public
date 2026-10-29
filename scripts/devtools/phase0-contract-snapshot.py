#!/usr/bin/env python3
"""phase0-contract-snapshot.py — Phase 0 契約の凍結と drift 検出。

plan slug:kura-web-componentization Step 1 (finding 5)。
v1.89.0 (commit 1fee52d02) で landed した Phase 0 抽出物の公開契約:

  - web/src/lib/bulkTag.ts            … exported symbol 名
  - web/src/lib/components/BulkTagBar.svelte  … $props フィールド集合 (+bindable)
  - web/src/lib/components/PoolTitleBar.svelte … $props + module export
  - web/src/lib/styles/bulk.css       … class 名 / CSS custom property / keyframes

を `web/tests/phase0-contract.json` に凍結する。後続 phase (P2 TagChip 以降) は
preflight でこのツールを引数なしで実行する — 現ツリーが snapshot と一致しなければ
非 0 で落ち、Phase 0 契約の無言 drift を build 時に検出する (runtime ではなく)。

usage:
  phase0-contract-snapshot.py            # 照合: 一致 → exit 0 / drift → exit 1
  phase0-contract-snapshot.py --write    # 現ツリーから snapshot を(再)生成
  phase0-contract-snapshot.py --root DIR # 別ツリーを対象にする (drift シミュレーション用)

契約を意図的に変えるときは --write → diff を人間レビュー → commit が唯一の経路。
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

BULKTAG = "web/src/lib/bulkTag.ts"
BULKTAGBAR = "web/src/lib/components/BulkTagBar.svelte"
POOLTITLEBAR = "web/src/lib/components/PoolTitleBar.svelte"
BULKCSS = "web/src/lib/styles/bulk.css"
SNAPSHOT = "web/tests/phase0-contract.json"

TS_EXPORT_RE = re.compile(
    r"^\s*export\s+(?:declare\s+)?(?:async\s+)?"
    r"(?:function|const|let|class|type|interface|enum)\s+([A-Za-z_$][\w$]*)",
    re.M,
)


def strip_comments(text: str) -> str:
    """/* */ と // コメントを除去 (契約抽出をコメント内の記述に汚染させない)。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    return text


def ts_exports(text: str) -> list[str]:
    return sorted(set(TS_EXPORT_RE.findall(strip_comments(text))))


def _match_braces(text: str, open_idx: int) -> int:
    """text[open_idx] == '{' の対応する '}' の index を返す。"""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("unbalanced braces")


def svelte_props(text: str) -> dict:
    """`let { ... } = $props()` の destructure からフィールド名を取る。
    `field = $bindable(...)` は bindable として別掲。"""
    src = strip_comments(text)
    m = re.search(r"let\s*\{", src)
    if not m or "$props()" not in src:
        return {"props": [], "bindable": []}
    open_idx = m.end() - 1
    close_idx = _match_braces(src, open_idx)
    body = src[open_idx + 1 : close_idx]
    props: list[str] = []
    bindable: list[str] = []
    # depth-0 のカンマで分割 (default 値に {} や () が来ても壊れないように)
    depth = 0
    part = ""
    parts: list[str] = []
    for ch in body:
        if ch in "{([":
            depth += 1
        elif ch in "})]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(part)
            part = ""
        else:
            part += ch
    if part.strip():
        parts.append(part)
    for p in parts:
        pm = re.match(r"\s*([A-Za-z_$][\w$]*)", p)
        if not pm:
            continue
        name = pm.group(1)
        props.append(name)
        if "$bindable" in p:
            bindable.append(name)
    return {"props": sorted(props), "bindable": sorted(bindable)}


def svelte_module_exports(text: str) -> list[str]:
    m = re.search(r'<script[^>]*\bmodule\b[^>]*>(.*?)</script>', text, re.S)
    if not m:
        return []
    return ts_exports(m.group(1))


def css_contract(text: str) -> dict:
    src = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    # 宣言ブロック内 (e.g. `0 12px`, `.82rem`) を class と誤認しないため
    # セレクタ部分 (`{` の外) だけを対象にする。
    selectors = re.sub(r"\{[^{}]*\}", " ", src)
    classes = sorted(set(re.findall(r"\.([A-Za-z_][A-Za-z_-][\w-]*)", selectors)))
    custom_props = sorted(set(re.findall(r"(--[\w-]+)\s*:", src)))
    keyframes = sorted(set(re.findall(r"@keyframes\s+([\w-]+)", src)))
    return {"classes": classes, "custom_properties": custom_props, "keyframes": keyframes}


def extract(root: Path) -> dict:
    return {
        "_meta": {
            "description": "Phase 0 (v1.89.0, 1fee52d02) frozen public contract — "
            "plan kura-web-componentization Step 1. Regenerate ONLY via "
            "scripts/devtools/phase0-contract-snapshot.py --write (human-reviewed).",
        },
        BULKTAG: {"exports": ts_exports((root / BULKTAG).read_text())},
        BULKTAGBAR: svelte_props((root / BULKTAGBAR).read_text()),
        POOLTITLEBAR: {
            **svelte_props((root / POOLTITLEBAR).read_text()),
            "module_exports": svelte_module_exports((root / POOLTITLEBAR).read_text()),
        },
        BULKCSS: css_contract((root / BULKCSS).read_text()),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true", help="現ツリーから snapshot を書く")
    ap.add_argument("--root", type=Path, default=None,
                    help="対象ツリー root (default: この script の repo root)")
    ap.add_argument("--snapshot", type=Path, default=None,
                    help="snapshot JSON の path (default: <repo>/web/tests/phase0-contract.json)")
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    root = args.root or repo_root
    snap_path = args.snapshot or (repo_root / SNAPSHOT)

    current = extract(root)
    current_s = json.dumps(current, indent=2, ensure_ascii=False, sort_keys=True) + "\n"

    if args.write:
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(current_s)
        print(f"wrote {snap_path}")
        return 0

    if not snap_path.exists():
        print(f"FAIL: snapshot missing: {snap_path} (run with --write first)", file=sys.stderr)
        return 2

    frozen_s = snap_path.read_text()
    try:
        frozen = json.loads(frozen_s)
    except json.JSONDecodeError as e:
        print(f"FAIL: snapshot unparseable: {e}", file=sys.stderr)
        return 2

    if frozen == current:
        print("phase0 contract OK (tree matches frozen snapshot)")
        return 0

    print("FAIL: Phase 0 contract drift detected (tree != frozen snapshot):",
          file=sys.stderr)
    frozen_norm = json.dumps(frozen, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    for line in difflib.unified_diff(
        frozen_norm.splitlines(keepends=True),
        current_s.splitlines(keepends=True),
        fromfile="frozen " + str(snap_path.relative_to(repo_root) if snap_path.is_relative_to(repo_root) else snap_path),
        tofile="current tree",
    ):
        sys.stderr.write(line)
    print("\n意図的な契約変更なら: --write → diff を人間レビュー → commit。",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
