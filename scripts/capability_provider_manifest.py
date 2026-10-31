#!/usr/bin/env python3
"""kura#66 G7 — capability provider manifest を*導出*する。

G7 の主張は「Kura core は capability を要求してよいが、特定実装・GPU・vendor・
remote service を要求してはならない」。これは設計意図であって、意図は tree の
中で真になっているとは限らない。ここで見るのは 4 つだけ、いずれも導出:

  1. **core が実装を import していないか** — core_boot_paths の中から
     implementation_paths の module を import していれば、capability は
     optional ではなく boot 依存。`disabled` 構成は成立しない
  2. **core の依存に推論 runtime が無いか** — core_requirements_file を読み、
     torch / onnxruntime 等が core の install 条件になっていないかを見る
  3. **`disabled` を表す設定が実在するか** — policy が宣言した config_flag が
     tree のどこにも無ければ、「disabled が正常構成」は宣言されていない
  4. **machine result が provenance を運ぶか** — writer が実際に書く field 集合
     (json.dumps の dict literal から AST で抽出) と、table の列 (migration
     本文) を、契約の required と突き合わせる

見ていない面は PASS にしない: 走査できなかった writer / 見つからなかった
migration は `unobserved` に載り、status は UNKNOWN 以上にならない。

出力は JSON (stdout)。`--strict` のときだけ status!=PASS で exit 1。read-only。
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_POLICY = REPO / "release" / "capability_policy.json"

_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*))", re.M
)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True
    ).stdout


def _tracked() -> list[str]:
    return [p for p in _git("ls-files").splitlines() if p]


def _match_any(path: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if fnmatch.fnmatch(path, pat) or (
            pat.endswith("/**") and path.startswith(pat[:-3] + "/")
        ):
            return True
    return False


def _impl_roots(patterns: list[str]) -> set[str]:
    """implementation_paths から import root 名を作る (`tagger/**` → `tagger`)。"""
    roots = set()
    for pat in patterns:
        root = pat.split("/")[0]
        if root and "*" not in root:
            roots.add(root)
    return roots


def _json_dumps_key_sets(path: Path) -> list[list[str]]:
    """`json.dumps({...})` に渡される dict literal の key 集合を全部返す。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []
    out: list[list[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = (
            f"{func.value.id}.{func.attr}"
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            else getattr(func, "id", "")
        )
        if name not in ("json.dumps", "dumps"):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Dict):
            keys = [
                k.value for k in arg.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            ]
            if keys:
                out.append(keys)
    return out


def _migration_mentions(table: str) -> list[Path]:
    versions = REPO / "api" / "alembic" / "versions"
    if not versions.is_dir():
        return []
    return [p for p in sorted(versions.glob("*.py"))
            if table in p.read_text(encoding="utf-8", errors="ignore")]


def scan(policy: dict) -> dict:
    tracked = _tracked()
    core_paths = policy.get("core_boot_paths", [])
    findings: list[dict] = []
    unobserved: list[str] = []

    core_files = [p for p in tracked if _match_any(p, core_paths) and p.endswith(".py")]

    for cap in policy.get("capabilities", []):
        cap_id = cap["id"]
        roots = _impl_roots(cap.get("implementation_paths", []))

        # 1. core → 実装への import
        for path in core_files:
            try:
                text = (REPO / path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                unobserved.append(f"{path} ({exc.__class__.__name__})")
                continue
            for m in _IMPORT_RE.finditer(text):
                mod = m.group(1) or m.group(2)
                if mod.split(".")[0] in roots:
                    findings.append({
                        "kind": "core_imports_capability",
                        "capability": cap_id,
                        "path": path,
                        "imports": mod,
                        "line": text[: m.start()].count("\n") + 1,
                    })

        # 3. disabled を表す設定
        flag = cap.get("config_flag")
        if flag:
            hits = subprocess.run(
                ["git", "-C", str(REPO), "grep", "-l", "--", flag],
                capture_output=True, text=True,
            ).stdout.split()
            hits = [h for h in hits if not h.startswith("release/")]
            if not hits:
                findings.append({
                    "kind": "no_disabled_configuration",
                    "capability": cap_id,
                    "flag": flag,
                    "detail": "宣言された config_flag が tree に存在しない — "
                              "`disabled` が正常構成として実装されていない",
                })

        # 4. result records の provenance
        for rec in cap.get("result_records", []):
            if rec.get("kind") == "jsonl":
                writer = REPO / rec["writer"]
                if not writer.exists():
                    unobserved.append(f"{rec['writer']} (writer が存在しない)")
                    continue
                key_sets = _json_dumps_key_sets(writer)
                if not key_sets:
                    unobserved.append(
                        f"{rec['writer']} (json.dumps の dict literal を抽出できず)"
                    )
                    continue
                observed = sorted({k for ks in key_sets for k in ks})
                missing = [f for f in rec["required_fields"] if f not in observed]
                if missing:
                    findings.append({
                        "kind": "result_without_provenance",
                        "capability": cap_id,
                        "record": rec["id"],
                        "writer": rec["writer"],
                        "observed_fields": observed,
                        "missing_fields": missing,
                        "issue": rec.get("issue"),
                    })
            elif rec.get("kind") == "table":
                mig = _migration_mentions(rec["id"])
                if not mig:
                    unobserved.append(f"{rec['id']} (この表を作る migration が見つからない)")
                    continue
                text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in mig)
                missing = [c for c in rec.get("provenance_columns", []) if c not in text]
                if missing:
                    findings.append({
                        "kind": "result_without_provenance",
                        "capability": cap_id,
                        "record": rec["id"],
                        "evidence": "static: migration 本文に列名が現れない",
                        "migrations": [p.name for p in mig],
                        "missing_fields": missing,
                    })

        # 2. core の依存に推論 runtime
        req_rel = policy.get("core_requirements_file")
        req = REPO / req_rel if req_rel else None
        if req and req.exists():
            body = "\n".join(
                line for line in req.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            ).lower()
            for tok in policy.get("forbidden_core_dependency_tokens", []):
                if tok.lower() in body:
                    findings.append({
                        "kind": "core_requires_inference_runtime",
                        "capability": cap_id,
                        "file": req_rel,
                        "token": tok,
                    })
        elif req_rel:
            unobserved.append(f"{req_rel} (core requirements を読めない)")

    ratified = bool(policy.get("ratified"))
    by_kind: dict[str, int] = {}
    for f in findings:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1

    reasons = [f"{k}: {v} 件" for k, v in sorted(by_kind.items())]
    if findings:
        status = "FAIL"
    elif unobserved or not ratified:
        status = "UNKNOWN"
        if not ratified:
            reasons.append("policy.ratified=false — capability contract が未裁定")
    else:
        status = "PASS"

    return {
        "kind": "capability_provider_manifest",
        "produced_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "subject_revision": _git("rev-parse", "HEAD").strip(),
        "policy_version": policy.get("policy_version"),
        "policy_ratified": ratified,
        "capabilities": [
            {
                "id": c["id"],
                "optional": c.get("optional"),
                "providers": [p["id"] for p in c.get("providers", [])],
                "config_flag": c.get("config_flag"),
            }
            for c in policy.get("capabilities", [])
        ],
        "findings": findings,
        "finding_counts": by_kind,
        "unobserved": unobserved,
        "status": status,
        "reasons": reasons,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()
    result = scan(json.loads(args.policy.read_text(encoding="utf-8")))
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 1 if args.strict and result["status"] != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
