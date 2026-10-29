"""issue #52 層 2 — migration merge をトリガに replica propagation task を自動生成する。

層 1 (配る中身の検証済み性) は docs/notes/replica-ddl-propagation.md の強制クラス
定義 + scripts/replica_schema_audit.sh。この module はそのテンプレートを機械生成
するだけで、手順を発明しない。0151 の実害 (生成器 = 人間は正しく動いたのに、
配られた手順から台帳の段が抜けて CRIT ×41 + vnext 全停止) の根治は「生成器を
作ること」ではなく「テンプレの完全性 — 特に台帳段 — を機械速度で保証すること」。

検知点は canonical の lifespan (deploy 経路は必ずここを通る — issue #52 の提案
どおり)。`alembic upgrade head` の前後で `kura_alembic_version` を比較し、進んで
いれば新 revision を列挙して 1 つの task にまとめ、broker 経由で trainer へ送る。

REPLICA_DDL の宣言契約 (0153 以降は pre-commit が宣言必須を強制):
    REPLICA_DDL: str | None
      None = publisher-only (replica は台帳 stamp のみ)
      str  = replica に先行適用すべき DDL。「apply が強制するクラス」のみ —
             列の追加/削除/型、CHECK、NOT NULL、PK/UNIQUE (runbook の表)。
    未宣言 (属性なし) = 0153 より前の世代。**黙って stamp 扱いにしない** —
    task 本文に手動分類の必要を明記する (fail-loud)。

fail 方針: broker 不達で kura-api の起動は止めない (replication 自体は
publisher-only なら安全)。ただし REPLICA_DDL が実 DDL を持つ revision を配れな
かったときだけ ERROR で残す — その窓は apply crash-loop になりうる (#52)。
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path

_log = logging.getLogger("kura.replica_propagation")

BROKER_DISPATCH_CLI = str(Path.home() / "broker" / "scripts" / "broker_dispatch_task.py")
TARGET_AGENT = "trainer-cc-primary"

_UNDECLARED = object()

#: publication のメンバー表の snapshot。SoT は publication 自身で、この file は
#: DB を持たない場所 (pre-commit) でも宣言ゲートが走るために在る。live との一致は
#: tests/test_replica_ddl_declaration.py が検査する。
PUBLISHED_TABLES_FILE = Path(__file__).resolve().parent / "replica_published_tables.txt"

# 「replica に無いと apply が壊れるクラス」だけを見る。index は replica local な
# ので落ちても apply は壊れない — 入れると偽陽性でゲートが無視されるようになる。
_OP_TABLE_ARG = re.compile(
    r"op\.(create_table|drop_table|rename_table|add_column|drop_column|alter_column)"
    r"\(\s*[\"']([a-z_0-9]+)[\"']")
_RAW_ALTER = re.compile(
    r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([a-z_0-9]+)\s+"
    r"(?=.*\b(?:ADD\s+COLUMN|DROP\s+COLUMN|ALTER\s+COLUMN|ADD\s+CONSTRAINT"
    r"|DROP\s+CONSTRAINT|SET\s+NOT\s+NULL))", re.I | re.S)
_RAW_CREATE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_0-9]+)", re.I)


def published_tables(path: Path | None = None) -> set[str]:
    """複製対象のテーブル名。file が無いのは「複製が無い」ではないので raise する。"""
    p = path or PUBLISHED_TABLES_FILE
    return {ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")}


def shape_changing_tables(source: str) -> set[str]:
    """migration の本体 (upgrade 以降) が形を変えているテーブル名。

    docstring の散文は読まない — 「replication について」節で表名を語っただけの
    migration を塞ぐと、ゲートは即座に無視される側に回る (0148/0150 の教訓)。
    """
    i = source.find("def upgrade")
    body = source[i:] if i >= 0 else source
    found = {m.group(2) for m in _OP_TABLE_ARG.finditer(body)}
    found |= {m.group(1) for m in _RAW_ALTER.finditer(body)}
    found |= {m.group(1) for m in _RAW_CREATE.finditer(body)}
    return found


def contradicts_publisher_only(source: str, published: set[str]) -> set[str]:
    """`REPLICA_DDL = None` と宣言しながら複製対象の形を変えている表を返す。

    issue #60 の self-stamp はこの宣言だけを信じて replica の台帳を進める。宣言が
    嘘だと replica は「列の無い最新」になり、台帳一致の監視は green のまま、
    publisher から行が届いた瞬間に apply worker が crash-loop する — 誤りが
    最も遅く、最も分かりにくい形で出る。宣言の存在ではなく**中身**を検査する。
    """
    m = re.search(r"^REPLICA_DDL\s*(?::[^=]*)?=\s*(.+)$", source, re.M)
    if not m or m.group(1).strip() != "None":
        return set()
    return shape_changing_tables(source) & published


def _numeric(rev: str | None) -> int | None:
    try:
        return int(rev) if rev is not None else None
    except (TypeError, ValueError):
        return None


def collect_new_revisions(pre_head: str, post_head: str) -> list[tuple[str, object]]:
    """(revision, REPLICA_DDL 宣言) を昇順で返す。宣言なしは _UNDECLARED。

    kura の revision は 4 桁連番 (vnext_runner._schema_drift_verdict と同じ前提)。
    連番でない revision が挟まっていたら例外 — 黙って読み飛ばすと「配ったつもり」
    が生まれる。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    pre_n, post_n = _numeric(pre_head), _numeric(post_head)
    if pre_n is None or post_n is None:
        raise ValueError(f"non-numeric revision bounds: {pre_head!r} -> {post_head!r}")
    sd = ScriptDirectory.from_config(Config("alembic.ini"))
    out: list[tuple[str, object]] = []
    for script in sd.walk_revisions():
        n = _numeric(script.revision)
        if n is None:
            raise ValueError(f"non-numeric revision in chain: {script.revision!r}")
        if pre_n < n <= post_n:
            out.append((script.revision,
                        getattr(script.module, "REPLICA_DDL", _UNDECLARED)))
    out.sort(key=lambda t: int(t[0]))
    return out


def all_publisher_only(revisions: list[tuple[str, object]]) -> bool:
    """issue #60: この revision 群は replica の台帳 self-stamp だけで済むか。

    **明示的な `REPLICA_DDL = None` 宣言のみ**を publisher-only と数える —
    未宣言 (_UNDECLARED) は「分類されていない」であって「DDL 不要」ではない
    (P4: 未判定と既定値を区別する)。空リストは「進める先が無い」= False。
    """
    return bool(revisions) and all(decl is None for _, decl in revisions)


def build_task_content(pre_head: str, post_head: str,
                       revisions: list[tuple[str, object]]) -> tuple[str, bool]:
    """runbook テンプレどおりの task 本文を組む。returns (content, has_ddl)。

    台帳段は生成側が必ず含める — 人間の記憶に運ばせない (0151 の根治点)。
    """
    ddl_parts: list[str] = []
    has_ddl = False
    for rev, decl in revisions:
        if decl is _UNDECLARED:
            has_ddl = True  # 分類不能は危険側に倒す
            ddl_parts.append(
                f"-- mig {rev}: ⚠ REPLICA_DDL 未宣言 (0153 より前の世代)。"
                f"docs/notes/replica-ddl-propagation.md の強制クラス定義"
                f" (apply が tuple 単位で強制するか) で手動分類してから適用すること。")
        elif decl is None:
            ddl_parts.append(f"-- mig {rev}: publisher-only — replica への DDL は不要 (stamp のみ)")
        else:
            has_ddl = True
            ddl_parts.append(f"-- mig {rev}:\n{str(decl).strip()}")
    body = "\n\n".join(ddl_parts)
    content = f"""kura replica schema DDL 伝播 (mig {pre_head} -> {post_head}, 自動生成 — issue #52 層 2, api/replica_propagation.py):
primary canonical は適用済み。trainer のローカル kura DB に以下を適用してください (psql -v ON_ERROR_STOP=1 で 1 トランザクション推奨)。

{body}

続けて台帳を進めてください (**この段が 0148/0151 の依頼で抜けて CRIT ×41 + vnext 全停止になった** — fleet invariant と vnext の schema drift 判定が読む)。
この段は自分で検算します — 素の UPDATE は台帳が中間値に居ると 0 行で黙って通り、
psql も exit 0 を返すため (#60 self-stamp が replica 自身の checkout head まで
進めるので、checkout が遅れていると中間値は通常状態):

  DO $$
  DECLARE n int; cur text;
  BEGIN
    UPDATE kura_alembic_version SET version_num = '{post_head}' WHERE version_num = '{pre_head}';
    GET DIAGNOSTICS n = ROW_COUNT;
    SELECT version_num INTO cur FROM kura_alembic_version;
    IF n = 0 AND cur IS DISTINCT FROM '{post_head}' THEN
      RAISE EXCEPTION '台帳が想定外の位置にあります: % (期待: {pre_head} から進める、または既に {post_head})。'
        'checkout が中間 revision で self-stamp された可能性 — この task の DDL 区間と'
        ' 台帳の現在地を突き合わせてから手で進めること', cur;
    END IF;
  END $$;

(publisher-only のみの区間は replica の kura-api が再起動時に self-stamp している
ことがある (#60) — 台帳が既に '{post_head}' なら上の DO は何もせず正常終了する。
中間値なら**上の DO が止める** ので、確認だけで complete しないこと)

確認: SELECT version_num FROM kura_alembic_version;  (= {post_head})
適用後、manage_task complete で報告を。手順の SoT: kura docs/notes/replica-ddl-propagation.md"""
    return content, has_ddl


def dispatch_propagation(pre_head: str, post_head: str) -> bool:
    """新 head 検知時に 1 つの propagation task を broker へ送る。fail-soft。"""
    try:
        revisions = collect_new_revisions(pre_head, post_head)
        if not revisions:
            return True
        content, has_ddl = build_task_content(pre_head, post_head, revisions)
        result = subprocess.run(
            [sys.executable, BROKER_DISPATCH_CLI,
             "--content", content,
             "--kind", "deploy",
             "--agent", TARGET_AGENT,
             "--dedup-key", f"replica-ddl:{pre_head}->{post_head}",
             "--importance", "high" if has_ddl else "medium",
             "--source-executor", "cc",
             "--allow-stale"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            _log.info("replica propagation task dispatched (%s -> %s, %d revision(s), has_ddl=%s)",
                      pre_head, post_head, len(revisions), has_ddl)
            return True
        _log.log(
            logging.ERROR if has_ddl else logging.WARNING,
            "replica propagation dispatch FAILED (%s -> %s, has_ddl=%s): rc=%d %s — "
            "trainer の apply worker が crash-loop しうる窓が開いている (has_ddl 時)。"
            "手動で runbook の手順を dispatch すること",
            pre_head, post_head, has_ddl, result.returncode,
            (result.stderr or result.stdout).strip()[:300])
        return False
    except Exception as e:  # noqa: BLE001 — 起動は止めない (docstring の fail 方針)
        _log.error("replica propagation generation failed (%s -> %s): %s — "
                   "手動で runbook の手順を dispatch すること", pre_head, post_head, e)
        return False
