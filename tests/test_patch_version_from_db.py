"""版番号の真実が DB であることを固定する。

■ 2026-08-05 実測の危険

`_next_version` は `tagger/patches/<tag>/v*.json` を数えていた。しかし版の
権威は governed な `tagger_patch` 行のほうで、ファイルは git に置いた写し。
両者はずれる:

    canonical のファイル: v7, v8, v9   → 次は v10 と計算
    DB の実際の最大版:    v10          ← 既にある

この状態で昇格すると **v10 を再発番**し、`ON CONFLICT DO UPDATE` で既存の
v10 を別の測定で**黙って上書き**する。

実際 dark_labia_minora がその状態だった — v10.json は nodec にしか存在せず、
canonical に無かった (配布経路が GitHub を迂回しており、primary の auto_review が
作った commit が push 前に nodec へ直接 pull されていたため)。

**ファイルの有無で版が変わると、チェックアウトごとに違う番号が出る。**
"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "tagger" / "auto_review.py").read_text()


def _fn(name: str) -> str:
    b = SRC[SRC.index(f"async def {name}("):]
    for end in ("\nasync def ", "\ndef "):
        i = b.find(end, 10)
        if i != -1:
            return b[:i]
    return b


def test_the_version_comes_from_the_database():
    b = _fn("_next_version")
    assert "FROM tagger_patch" in b, "版を DB から採っていない"
    assert "max(version)" in b


def test_the_version_does_not_come_from_files():
    """ファイルを数えると、チェックアウトごとに違う番号が出る。"""
    b = _fn("_next_version")
    code = "\n".join(l for l in b.splitlines() if not l.lstrip().startswith("#"))
    code = code.replace('"""', "\x00", 1)
    code = code[:code.index("\x00")] + code[code.rindex('"""') + 3:] if '"""' in code else code
    assert "glob(" not in code, "まだファイルを数えている"
    assert "PATCHES" not in code


def test_it_is_called_with_the_db():
    assert "await _next_version(db, tag)" in SRC


def test_the_promotion_still_writes_the_file():
    """DB を権威にするが、git 上の写しは残す (人が読める記録)。
    **消すのは別の判断**なので、ここでは落とさない。"""
    body = SRC[SRC.index("async def promote_validated("):]
    assert "json_path.write_text(" in body


# ── 同じ bytes には同じ版 ────────────────────────────────────────────────
#
# 版は「どのモデルか」を指すもので「何回昇格したか」ではない。実測 2026-08-05:
#
#     dark_labia_minora v9 / v10 → patch_sha256 が同一 (623c8fc41a3c…)
#     smegma            v2..v7   → 3 版ずつ同じ bytes
#
# identity は中身の hash — artifact ではその原則を守っているのに、版番号だけ
# 通し番号だった。**同じ bytes が別の名前で増える**。

def test_the_same_bytes_reuse_the_same_version():
    b = _fn("_version_for")
    assert "patch_sha256 = :s" in b, "同じ sha の既存版を探していない"
    assert "min(version)" in b


def test_a_new_artifact_gets_a_new_version():
    b = _fn("_version_for")
    assert "_next_version(db, tag)" in b


def test_no_artifact_hash_still_gets_a_version():
    """sha が無い行 (旧形式) でも昇格を止めない。"""
    b = _fn("_version_for")
    assert "if artifact_sha256:" in b


def test_promotion_uses_it():
    assert '_version_for(db, tag, r["artifact_sha256"])' in SRC
