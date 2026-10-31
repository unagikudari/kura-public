"""同一バイトの並行アップロードは、500 ではなく duplicate で返る。

2026-08-31 実測。ある creator は 1 枚の共通バナー画像を多数の post の
1 ページ目に使っている。post ごとに取り込むと、同一ファイルが同じ並列 run の中で
何度もアップロードされる。

競合の形:

  1. md5 の dedup は SELECT。両者ともまだ相手の行を見ない
  2. 両者 INSERT (この時点で sha256 は NULL)
  3. `_synchronous_materialize` が原本の sha256 を入れる — 同一バイトなので同値
  4. COMMIT で `sha256` の一意制約に当たり、負けた方が 500

`checksum_md5` 側のコメントが同じ TOCTOU を既に記録している (一意索引が無いので
重複行 124 群 / 1792 行が生まれている)。**どちらの列に一意索引があるかで結果が
変わるだけ**で、索引がある sha256 側では 500 になりアップロードが失われる。

2019 ファイルのうち 1 件がこれで落ち、その post の pool が作られなかった。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = (REPO / "api" / "routers" / "upload.py").read_text(encoding="utf-8")


def _load(name: str):
    """upload.py から関数 1 つだけを取り出す (fastapi 等の依存を持ち込まない)。

    見つからなければ落とす — 空を返すと、判定器が消えていても素通りする。
    """
    tree = ast.parse(SRC)
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    assert fn is not None, f"{name} が upload.py に無い"
    ns: dict = {"IntegrityError": Exception}
    exec(compile(ast.Module([fn], []), "<f>", "exec"), ns)
    return ns[name]


class _Exc(Exception):
    def __init__(self, orig): self.orig = orig


class _Orig:
    """What SQLAlchemy's asyncpg adapter puts in IntegrityError.orig."""
    def __init__(self, sqlstate, constraint, message, table="post"):
        self.sqlstate = sqlstate
        self.driver_exception = type("UniqueViolationError", (),
                                     {"constraint_name": constraint, "table_name": table})()
        self.message = message

    def __str__(self):
        return self.message


JA = '重複したキー値は一意性制約"ux_post_sha256_ok"違反となります\nDETAIL:  キー (sha256)=(\\x3780) はすでに存在します。'


@pytest.mark.parametrize("orig", [
    _Orig("23505", "ux_post_sha256_ok", JA),   # production: lc_messages = ja_JP.UTF-8
    _Orig("23505", "ux_post_sha256_ok",
          'duplicate key value violates unique constraint "ux_post_sha256_ok"'),
    _Orig("23505", "test_unique_sha256", "anything at all"),
])
def test_a_sha256_unique_violation_is_recognised(orig):
    """By SQLSTATE and the constraint's name — the message's language does not matter.

    Until 2026-10-25 this matched English words in the message, and production's
    Japanese messages never contained them: the race path re-raised as a 500."""
    assert _load("_is_sha256_conflict")(_Exc(orig)) is True


@pytest.mark.parametrize("orig", [
    _Orig("23505", "tag_name_name_key", 'duplicate key value violates unique constraint "tag_name_name_key"'),
    _Orig("23505", "ux_pool_url_url", JA.replace("ux_post_sha256_ok", "ux_pool_url_url")),
    _Orig("23502", None, 'null value in column "user_id" violates not-null constraint'),
    _Orig("23505", "uq_lora_asset_sha256", "another table's digest", table="lora_asset"),
    _Orig("23505", "blob_sha256_key", "another table's digest", table="blob"),
    _Orig("23503", None, 'insert or update on table "post" violates foreign key constraint'),
])
def test_every_other_integrity_error_still_raises(orig):
    """狭く判定すること。全部を duplicate として飲み込むと、将来足される制約が
    どれも『正常な重複』として黙って通る — この endpoint が繰り返し噛まれて
    いるのは、まさに『エラーが正常な結果に見える』形。"""
    assert _load("_is_sha256_conflict")(_Exc(orig)) is False


def test_the_match_needs_the_violation_not_just_the_word():
    """sha256 という語が出るだけの無関係なエラーを飲み込まない。"""
    f = _load("_is_sha256_conflict")
    assert f(_Exc(_Orig(None, None, "could not compute sha256: I/O error"))) is False
    assert f(_Exc("could not compute sha256: I/O error")) is False


# --- 呼び出し側の結線 ---------------------------------------------------------

def test_the_commit_is_guarded():
    assert "except IntegrityError as exc:" in SRC, "commit が保護されていない"
    assert "if not _is_sha256_conflict(exc):\n            raise" in SRC, \
        "無関係な IntegrityError を握り潰している"


def test_the_loser_resolves_to_the_winner_and_says_duplicate():
    """負けた側は勝者の行を返す。勝者が書いた行こそ、この呼び出し元が
    欲しかったものだから。"""
    assert 'winner = await ingest(db, data, existing_only=True, **fields)' in SRC
    assert 'return Ingested(existing, md5, sha256, True, dup_materialized)' in SRC
    assert '"duplicate": result.duplicate' in SRC


def test_the_orphaned_files_are_removed():
    """rollback すると post_id が解放され、その id で書いたファイルは
    誰からも参照されない。ディスク使用量の監査でしか見つからない孤児を
    残さない。"""
    assert "_discard(result.written)" in SRC
    assert "orphan.unlink(missing_ok=True)" in SRC


def test_a_vanished_winner_raises_rather_than_inventing_an_id():
    """勝者の行が消えていたら、id をでっち上げずに raise する。"""
    assert "if winner is None:" in SRC
    i = SRC.index("if winner is None:")
    assert "raise" in SRC[i:i + 400]


def test_the_race_is_documented_where_the_md5_twin_is():
    """同じ TOCTOU が md5 側にも在ることを、次の読み手が繋げられるように。"""
    assert "Non-upload writers" in SRC
    assert "checksum_md5" in SRC
