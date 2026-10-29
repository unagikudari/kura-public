"""0164 が作った partial index が **存在し、valid で、実際に使われている**こと。

この file は `test_slow_query_watch_scopes_to_this_database.py` から分離されました
(#136 round 6-7、trainer-gemini-primary の Finding 3)。分離前は 868 行の 1 file に

  ・監視スクリプト `slow_query_watch.py` が正しい母集団を見ているか
  ・0164 の索引が効いているか

という **2 つの責務が癒着**しており、reviewer の診断は「テスト内 helper の AST を
テスト内で構文解析するメタ検査は構造的袋小路」でした。round 4〜7 で「自作の AST 検査が
正しい実装を落とす」を **3 度**起こしています:

  ・`inspect.getsource` の全文検索が、その穴を説明するコメントの `to_regclass` に当たった
  ・`any('schema' in q)` が、存在検査の `current_schemas` に当たって常に真になった
  ・`(?:WHERE|AND)[^;]*current_schemas` が `ORDER BY` を跨いで正しい実装を赤くした

**検査対象と、検査対象についての記述を混ぜる**という 1 つの形です。分離にあたって
AST メタ検査 (`test_the_ledger_is_found_anywhere_and_read_where_it_was_found`) は
**廃止**し、`_FakeCursor` による**振る舞いの検査**に置き換えました —— 規則は
「どう書かれているか」ではなく「どう振る舞うか」で固定します。

なぜ「文字列ごと」の一致を要求するのか: partial index は「query の述語が index の述語を
含意する」と planner が証明できる時にしか使われず、PostgreSQL の証明器は
`status IS NULL` → `status IS DISTINCT FROM 'ok'` を渡りません。論理的に同値でも綴りが
違えば index は黙って使われなくなり、10 分ごとに 100 万行を読む状態へ **どこも赤く
ならずに** 戻ります。
"""
from __future__ import annotations

import functools
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


_INDEX_NAMES = ("ix_post_not_ok_not_terminal", "ix_post_not_ok_not_terminal_id",
                "ix_post_status_null", "ix_post_needs_fingerprint")

_INDEX_MIGRATION = "0164"


@functools.lru_cache(maxsize=None)
def _SCAN_USING(name: str) -> re.Pattern[str]:
    """plan の中で、その index を **scan node が名指ししている** ことだけに一致する。

    EXPLAIN の text 出力は scan の種類で語順が変わる:

        Index Scan using <name> on <table>
        Index Only Scan using <name> on <table>
        Index Scan Backward using <name> on <table>        ← 降順走査で `Backward` が挟まる
        Index Only Scan Backward using <name> on <table>
        Parallel Index Scan using <name> on <table>        ← 接頭辞なので部分一致で拾える
        Bitmap Index Scan on <name>                        ← `using` が無い

    素の `<name> in plan` は部分文字列で誤って一致し (`ix_post_not_ok_not_terminal` は
    `..._id` の部分文字列)、`using <name> on` 決め打ちは bitmap を取りこぼす。どちらも
    PR #126 / #136 で実地に踏んだ。scan node を名指しし、末尾を `\\b` で閉じる形が両方を
    同時に満たす — `_` は `\\w` なので `\\b` は `..._id` の手前で切れない。

    **`Backward` は #136 round 2 (trainer-gemini-primary) の指摘。** round 1 で私は 3 形を
    列挙して「これで網羅した」と書いたが、また漏れていた。`ORDER BY ... DESC` を sweeper に
    入れた瞬間、**index が理想的に効いているのに偽赤**になる。列挙で網羅を主張した回数が
    これで 2 回、外した回数も 2 回なので、列挙ではなく**省略可能な部分を文法として書く**。

    `Bitmap Index Scan` に `Backward` は無い (bitmap に走査方向の概念が無いため) ので、
    そちらは分けたまま。
    """
    return re.compile(
        rf"(?:Index(?: Only)? Scan(?: Backward)? using|Bitmap Index Scan on) "
        rf"{re.escape(name)}\b", re.I)
def _index_migration_applied(cur) -> bool:
    """kura_alembic_version が 0164 に達しているか。

    `kura_alembic_version` が実台帳 (`alembic_version` は szurubooru の遺物)。
    version 文字列は先頭 4 桁が連番なので、そこだけを数として比べる。

    **#136 round 2 (trainer-gemini-primary) の Finding 2/3 を反映。** 元の形は

        try:
            cur.execute("SELECT version_num FROM kura_alembic_version")
        except Exception:
            return False

    で、「台帳が無い」だけでなく **接続断・権限不足・transaction abort・catalog 破損まで
    すべて False** に落としていた。False は skip に倒れるので、DB 側の障害が丸ごと
    「未適用だから測れない」に化ける。この file が直している当の故障の型が、その判定関数の
    中にあったことになる。

    台帳の**不在**は例外ではなく catalog 引きで見る。それ以外の失敗は握り潰さず
    上へ投げる — 測れなかったことを緑にも skip にもしない。

    複数 head (Alembic の枝分かれ) では行が複数ある。`fetchone()` は物理走査順次第で古い方を
    拾いうるので `max()` を取る。現状 0001〜0164 の単一連番であることは reviewer が確認済みだが、
    非決定な読み取りを残す理由が無い。
    """
    # round 3 (trainer-gemini-primary) F1: `to_regclass('kura_alembic_version')` は
    # **スキーマ未修飾なので search_path に依存する**。`SET search_path TO pg_catalog` 下では
    # 台帳が実在しても NULL が返り、適用済み DB が「未適用だから skip」に倒れる (実測済み)。
    # round 2 で「不在は例外でなく NULL で見る」と直した結果、**私が新しい偽 skip を作った**。
    # 存在は catalog を直接見る — search_path に依存しない。
    # **存在検査と読み取りは、同じ 1 つの名前を指すこと。** この file はこの取り違えを
    # 3 度やっている:
    #   round 3: 不在を `to_regclass` の NULL で見た      → 未修飾なので search_path 依存
    #   round 4: `pg_class` を無条件に引いた              → 読み取りが未修飾のまま食い違う
    #   round 5: `current_schemas(false)` に絞った        → **round 3 の偽 skip が再発**
    #
    # round 5 (trainer-gemini-primary) Finding 2 の実測: 台帳が `public` にあっても
    # search_path に `public` が無ければ「未適用」と判定して skip する。しかも index が
    # 正常な DB では後続の EXPLAIN が別の理由で赤くなるので、**「index があれば赤、
    # 全滅していれば緑」という完全な逆転**になっていた。
    #
    # 読み取りを schema 修飾する以上、**見えるかどうかは関係ない** —— DB のどこかに台帳が
    # あるなら、その schema を名指しして読めばよい。search_path は「複数ある場合にどれを
    # alembic が使ったか」の推定にだけ使う。
    #
    # Finding 1: `ANY(...) LIMIT 1` は `ORDER BY` が無く非決定だった (reviewer が s1/s2/public
    # で実測し、本来の解決先 s1 ではなく物理順の public が返ることを確認)。search_path の
    # 順に並べ、そこに無いものは後ろへ回し、最後は名前順で決定的にする。
    # **この判定は round 5 → 6 → 7 で逆向きに押されている。** 各 round の反例はどれも実在する:
    #
    #   round 5: search_path で絞ると、台帳が public にあって search_path に無い DB で
    #            「未適用」と読んで skip する → 最悪の破損が隠れる
    #   round 6: 絞らないと、`backup` schema の古い台帳を拾って **偽赤**になる
    #   round 7: 絞ると、CVE-2018-1058 以降の標準構成 (アプリ表を `kura` schema に置き
    #            `search_path = kura, pg_catalog`、台帳は `public` に残る) で
    #            **`post` は見えるのに台帳が見えない** → round 5 の偽 skip が再発
    #
    # round 6 で私は「`post` と同じ解決の仕方で引く」という規則を立てたが、**round 7 の
    # 反例がそれを壊した** —— `post` が見えて台帳が見えない構成は普通にある。
    #
    # 立て直す。台帳は **database ごとに 1 つ**であって schema ごとではない (alembic は
    # 接続の search_path が指す先に 1 つ作る)。だから問うべきは「この接続から見えるか」
    # ではなく「**この database に台帳が幾つあるか**」:
    #
    #   0 個 → 未適用。skip (測れない)
    #   1 個 → それが台帳。schema を名指しして読む (search_path に無くても)
    #   2 個以上 → search_path 上のものを採る。無ければ **どれか分からないので落とす**
    #              (黙って選ぶのも、黙って skip するのも、どちらも報告すべきでない)
    #
    # これで round 5 と round 7 の反例は消える (どちらも台帳 1 個)。**round 6 の反例
    # (`backup` にだけ台帳がある) は 1 個なので「適用済み」と読む** —— そこだけは
    # 判断が割れており、reviewer に裁定を仰いでいる。私の読みは「1 つしかない台帳は
    # backup schema にあってもその DB の台帳」だが、確信は強くない。
    cur.execute(
        "SELECT n.nspname,"
        "       array_position(current_schemas(true), n.nspname::text) AS on_path"
        "  FROM pg_class c"
        "  JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE c.relname = 'kura_alembic_version' AND c.relkind IN ('r', 'p')"
        " ORDER BY on_path NULLS LAST, n.nspname")
    ledgers = cur.fetchall()
    if not ledgers:
        return False                        # この database に台帳が無い = 未適用
    if len(ledgers) > 1 and ledgers[0][1] is None:
        raise AssertionError(
            f"この database に `kura_alembic_version` が {len(ledgers)} 個あり、"
            f"どれも search_path 上に無い: {[r[0] for r in ledgers]}。"
            "どれがこの DB の台帳か判定できないので、黙って選ぶことも skip することも "
            "しない。search_path を明示するか、余分な台帳を片付けること。")
    row = (ledgers[0][0],)
    # schema 名に `"` を含む DB は合法 (`CREATE SCHEMA "a""b"`)。二重化して閉じる。
    schema = row[0].replace('"', '""')

    # 在るのに読めないなら、それは設定の不整合。**skip に倒さず投げる** (round 4 で
    # reviewer が「index があれば台帳は読まれないので replica でも偽赤にならない」と
    # 実測して APPROVE)。
    # round 3 F3: `max(version_num)` は SQL の**文字列**比較なので、Alembic の枝分かれで
    # 英字 revision (`0164_hotfix`、hex head) が混ざると辞書順で英字を拾い、数値抽出が
    # 失敗して未適用扱いになる。全 head を取り、Python 側で数値だけを見る。
    # `any` であって `all` ではない —— 古い head が残る DB で「0164 適用済みかつ index 全滅」
    # という最悪の破損が無言の SKIP に化けるため (round 4 で APPROVE)。
    cur.execute(f'SELECT version_num FROM "{schema}".kura_alembic_version')
    numbers = [int(m.group(1)) for (v,) in cur.fetchall()
               if v and (m := re.match(r"(\d+)", str(v)))]
    return any(n >= int(_INDEX_MIGRATION) for n in numbers)
def _missing_is_a_failure_not_a_skip(cur, rows) -> None:
    """index が 1 本も無いとき、skip してよいのは **migration が未適用のとき だけ**。

    trainer-gemini-primary が #136 round 1 で実測した穴 (Finding 4)。元の形は

        if not rows:
            pytest.skip("0164 未適用の DB")

    で、`kura_alembic_version` が 0164 に達しているのに 4 本とも無い DB —— つまり
    **考えうる最悪の故障** —— が、赤ではなく無言の SKIP に化けていた。reviewer の
    環境がまさにその状態で、この test は何も言わずに通った。

    「測れないから skip」と「測ったら全滅」を取り違えないこと。この file が 3 度
    踏んでいる族 (**緑は対象の性質ではなく runner の性質**) の、skip 版である。
    """
    if rows:
        return
    if _index_migration_applied(cur):
        raise AssertionError(
            f"kura_alembic_version は {_INDEX_MIGRATION} に達しているのに、"
            f"{_INDEX_MIGRATION} の index が 1 本も無い: {sorted(_INDEX_NAMES)}。"
            "migration が success を報告して何も作らなかったか、後から drop された。"
            "sweeper は 10 分ごとに全表を読んでいる。")
    pytest.skip(f"{_INDEX_MIGRATION} 未適用の DB")
def _dsn() -> str | None:
    """実 DB。predicate を PostgreSQL 自身に評価させるために要る。"""
    for env in ("KURA_TEST_DATABASE_URL", "DATABASE_URL"):
        if os.environ.get(env):
            return os.environ[env].replace("+asyncpg", "")
    env_file = REPO / "api" / "env"
    if env_file.is_file():
        m = re.search(r"^DATABASE_URL=(.*)$", env_file.read_text(), re.M)
        if m:
            return m.group(1).strip().strip('"').strip("'").replace("+asyncpg", "")
    return None
@pytest.mark.skipif(not _dsn(), reason="kura DB DSN が無い")
def test_the_0164_indexes_are_valid_not_merely_present():
    """0164 の 4 本が `indisvalid` であること — 存在するだけでは planner は使わない。

    PR #126 の review (trainer-cc-primary) が挙げた finding 1。migration は
    `CREATE INDEX CONCURRENTLY IF NOT EXISTS` を使う。concurrent build が中断されると
    index は残るが `indisvalid=false` になり、**次の migration 実行は `IF NOT EXISTS` で
    それを黙って飛ばす**。migration は success を報告し、planner はその index を一生使わない。
    「在るか」を見る検査は緑のまま — 10 分ごとに 100 万行を読む状態へ、どこも赤くならずに戻る。

    reviewer は「primary で最も確かめたかったが確かめられなかった」と書いている。ここに置けば、
    DB がある場所で走るたびに確かめられる。

    2026-09-11 primary 実測: 4 本とも valid。サイズは 8KB ではなく
    ix_post_not_ok_not_terminal 104 kB / _id 80 kB / 他 2 本 8192 bytes —
    `create_post` の NULL 流入と `'pending'` が述語に一致する分、母集団は空ではない。
    """
    import psycopg2

    cx = psycopg2.connect(_dsn())
    cx.autocommit = True
    try:
        with cx.cursor() as cur:
            cur.execute(
                "SELECT c.relname, i.indisvalid, i.indisready"
                "  FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
                " WHERE c.relname = ANY(%s)",
                (list(_INDEX_NAMES),))
            rows = {name: (valid, ready) for name, valid, ready in cur.fetchall()}
            # cursor が生きているうちに判定する — 「全滅」と「未適用」の区別に
            # kura_alembic_version の読みが要る。
            _missing_is_a_failure_not_a_skip(cur, rows)
    finally:
        cx.close()

    broken = {n: rows[n] for n in rows if not (rows[n][0] and rows[n][1])}
    assert not broken, (
        f"0164 の index が INVALID/NOT READY: {broken}。`CREATE INDEX CONCURRENTLY "
        f"IF NOT EXISTS` は壊れた index を黙って飛ばすので、migration を再実行しても直らない。"
        f"REINDEX INDEX CONCURRENTLY <name> で作り直すこと。")
    missing = sorted(set(_INDEX_NAMES) - set(rows))
    assert not missing, f"0164 の index が存在しない: {missing}"
SMALL_TABLE = 10_000


@pytest.mark.skipif(not _dsn(), reason="kura DB DSN が無い")
def test_the_sweeper_predicates_still_match_their_indexes():
    """0164 の partial index は、sweeper の述語を **文字列ごと** 写して効いている。

    partial index は「query の述語が index の述語を含意する」と planner が証明できる
    時にだけ使われる。sweeper 側の述語を書き換えると index は黙って使われなくなり、
    10 分ごとに 100 万行を読む状態 (orphan check: rows=0 に 1,185ms / 992,025 buffers)
    にそのまま戻る — どこも赤くならずに。

    述語は **script から読む**。ここに写経すると、写経と本体が別々に古くなり、その
    ずれこそが検出したい故障になる。5 本すべてを見るのは、4 本の index が別々の
    述語・別々の順序に対応していて、1 本だけ確かめると残りが黙って落ちるため
    (最初の版が実際にそうで、id 順の 2 本と needs-fp の 2 本は被覆されていなかった)。
    """
    import psycopg2

    def _predicate(script: str, name: str) -> str:
        """script の中の述語定数を、括弧内の文字列リテラルを繋いで復元する。"""
        src = (REPO / "scripts" / script).read_text(encoding="utf-8")
        m = re.search(rf"{name} = \(\s*(.*?)\s*\)\n", src, re.S)
        assert m, f"{script} の {name} が読めない — 定数の形が変わった"
        joined = "".join(re.findall(r'"([^"]*)"', m.group(1)))
        assert joined.strip(), f"{script} の {name} から述語を復元できない"
        return joined

    not_ok = _predicate("backfill_media.py", "NOT_OK_NOT_TERMINAL")
    needs_fp = _predicate("backfill_fingerprints.py", "NEEDS_FP")
    # round 1 (nodeb-cc-primary) F4: ここだけ述語が写経されていた。この test の
    # docstring が「写経すると本体と別々に古くなる」と宣言している当の故障が、
    # 5 本目にだけ残っていた。PROMOTE_NONIMG も同じ手で読める。
    promote = _predicate("backfill_fingerprints.py", "PROMOTE_NONIMG")

    #: (ラベル, 実際に sweeper が流す形の query, **許容される index 名の集合**)
    #:
    #: 集合にしているのは、planner が同じ述語の複数 index から正当に選ぶ case が実在する
    #: から。1 本に決め打つと正当な選択で赤くなり、部分文字列で緩めると別 index でも
    #: 通ってしまう (PR #126 でその両方を順に踏んだ)。
    CASES = [
        ("orphan rows",
         f"SELECT id, creation_time FROM post WHERE {not_ok} "
         f"AND creation_time < (now() AT TIME ZONE 'utc') - INTERVAL '30 minutes' "
         f"ORDER BY creation_time LIMIT 20",
         ("ix_post_not_ok_not_terminal",)),
        # count(*) は列を要さないので、planner は同じ述語の **どちらの** not-ok index にも
        # 落ちうる。実測 (2026-09-11 primary): `Index Only Scan using
        # ix_post_not_ok_not_terminal_id`。round 2 で私はこの緩和を「到達不能」と判断して
        # 消したが、実際は到達していた — 素の部分文字列一致が黙って肩代わりしていたので、
        # 消しても緑のままで誤りに気づけなかった。
        ("orphan count",
         f"SELECT count(*) FROM post WHERE {not_ok}",
         ("ix_post_not_ok_not_terminal", "ix_post_not_ok_not_terminal_id")),
        ("id,type",
         f"SELECT id, type FROM post WHERE {not_ok} ORDER BY id LIMIT 20",
         ("ix_post_not_ok_not_terminal_id",)),
        ("needs-fp rows",
         f"SELECT id, type FROM post WHERE {needs_fp} ORDER BY id LIMIT 20",
         ("ix_post_needs_fingerprint",)),
        ("promote UPDATE",
         f"SELECT id FROM post WHERE {promote}",
         ("ix_post_status_null",)),
    ]

    cx = psycopg2.connect(_dsn())
    cx.autocommit = True
    missed = []
    try:
        with cx.cursor() as cur:
            cur.execute("SELECT indexname FROM pg_indexes WHERE indexname = ANY(%s)",
                        (list(_INDEX_NAMES),))
            present = {r[0] for r in cur.fetchall()}
            # ここも同じ穴だった: 1 本を見て無ければ「未適用」と決めていた。
            # migration が適用済みなのに全滅している DB は赤にする。
            # (`present` が空なら下は必ず raise か skip で抜けるので、この後に
            #  `if not present: skip` を置くと到達不能。round 2 でその死にコードを指摘された。)
            _missing_is_a_failure_not_a_skip(cur, present)
            # The planner's choice says something only on a populated table: on a fresh
            # install's few rows any scan is as cheap, and even with seq scans priced
            # out it picks ix_post_creation_time (Ubuntu 26.04, 2026-10-26). Say so
            # instead of failing or passing on a table this small.
            cur.execute("SELECT reltuples FROM pg_class WHERE oid = 'post'::regclass")
            if (cur.fetchone()[0] or 0) < SMALL_TABLE:
                pytest.skip(f"post has under {SMALL_TABLE} rows — the planner's index choice "
                            "is not evidence on a table this small; checked on a populated database")
            for label, sql, want in CASES:
                cur.execute("EXPLAIN " + sql)
                plan = "\n".join(line for (line,) in cur.fetchall())
                # count(*) は列が要らないので別 index の Index Only Scan に落ちうる。
                # 求めているのは「0164 の述語 index のどれかが効いていること」で、
                # 特定の 1 本ではない。
                # round 1 F3: 以前は `want.startswith(基底名) and 基底名 in plan` で
                # 緩和しており、基底名が `_id` 版の**部分文字列でもある**ため、want が
                # `_id` 版でも基底名だけの plan で通っていた。`_id` 版を migration から
                # 消しても緑のまま = 固定は 3 系統だった。
                # round 2 で「絞った緩和は到達不能」と指摘され、実際そのとおりだった
                # (want == 基底名 かつ 基底名 in plan なら want not in plan が偽)。
                #
                # **ただし素の `want in plan` はまだ部分文字列一致だった** (trainer-cc-primary、
                # PR #126 の遅延 review が changelog から推論し、実測で裏が取れた): 基底名
                # `ix_post_not_ok_not_terminal` は `..._id` の部分文字列なので、creation_time 版を
                # migration から落として planner が `_id` 版 + Sort に移っても、基底名の assert は
                # 緑のまま通る。私は F3 で「明示的な緩和」を消しただけで、暗黙の緩和を残し、
                # しかも「素の一致に一本化する」と書いていた。**同じ族がこの file で 3 度目。**
                #
                # そこで一度 `using {name} on` に絞ったが、**その根拠は偽だった。**
                # ここには「EXPLAIN は必ず `Index Scan using <name> on <table>` の形で出す」と
                # 書いてあった。出さない。scan node は 3 形あり、bitmap だけ語順が違う:
                #
                #     Index Scan using <name> on <table>
                #     Index Only Scan using <name> on <table>
                #     Bitmap Index Scan on <name>          ← `using` が無い
                #
                # planner が bitmap を選ぶのは異常時ではなく通常の cost 判断で、visibility map
                # が汚れていれば本番で普通に起きる。つまり **index が正しく効いているのに赤**
                # になる。trainer-gemini-primary が #136 round 1 で指摘し、primary の本番 kura DB
                # (PostgreSQL 18.6, post 約 106 万行) で私も再現した:
                #
                #     ->  Bitmap Index Scan on ix_post_not_ok_not_terminal_id
                #     `using ix_post_not_ok_not_terminal_id on` in plan  →  False
                #
                # 教訓は substring の件と同じ向きを向いている。私は「区切りさえすれば安全」と
                # 思って、**区切りの綴りを実測せずにコメントで断定した**。部分文字列一致は
                # 緩すぎ、`using` 決め打ちは狭すぎ、どちらも根拠は私の思い込みだった。
                #
                # 3 形すべての scan node を名指しし、名前の末尾は `\b` で閉じる。`_` は `\w` なので
                # `ix_post_not_ok_not_terminal\b` は `..._id` に一致しない — 部分文字列事故は
                # 閉じたまま、bitmap の偽赤だけが消える。scan node に錨を打つので、index 名が
                # plan の別の位置に現れても拾わない。
                if not any(_SCAN_USING(name).search(plan) for name in want):
                    missed.append((label, want, plan.splitlines()[:3]))
    finally:
        cx.close()

    assert not missed, (
        "sweeper が 0164 の partial index を使っていない。述語がずれると全表走査に "
        f"戻る (誰も気づかない形で): {missed}")
_PLAN_FIXTURES = [
    # (ラベル, plan 断片, その index 名で一致すべきか)
    ("Index Scan",
     "Index Scan using ix_post_not_ok_not_terminal on post  (cost=0.29..8.31)", True),
    ("Index Only Scan",
     "Index Only Scan using ix_post_not_ok_not_terminal on post", True),
    # 2026-09-12 primary の本番 kura DB (PostgreSQL 18.6) から採った実物。
    # `using` が無いので、旧 `using {name} on` 判定はここで偽赤になっていた。
    ("Bitmap Index Scan",
     "->  Bitmap Index Scan on ix_post_not_ok_not_terminal  (cost=0.00..1.23 rows=3943)",
     True),
    # 別 index。`ix_post_not_ok_not_terminal` は `..._id` の部分文字列なので、
    # 素の `in` 判定はここで誤って緑になっていた。
    ("別 index (_id) を基底名で問う",
     "->  Bitmap Index Scan on ix_post_not_ok_not_terminal_id  (cost=0.00..1.23)", False),
    # scan node でない位置に名前が現れても拾わない。
    ("scan node 以外の言及",
     "Recheck Cond: (...)  /* ix_post_not_ok_not_terminal を参照するコメント */", False),
    # round 2 (trainer-gemini-primary) が挙げた漏れ。`ORDER BY ... DESC` で planner が
    # 降順走査を選ぶと `Backward` が挟まり、round 1 の 3 形列挙では偽赤になっていた。
    ("Index Scan Backward",
     "Index Scan Backward using ix_post_not_ok_not_terminal on post  (cost=0.29..446.29)",
     True),
    ("Index Only Scan Backward",
     "Index Only Scan Backward using ix_post_not_ok_not_terminal on post", True),
    ("Parallel Index Scan",
     "->  Parallel Index Scan using ix_post_not_ok_not_terminal on post", True),
    ("Parallel Index Scan Backward",
     "->  Parallel Index Scan Backward using ix_post_not_ok_not_terminal on post", True),
    # Backward を足したことで新しい偽緑を作っていないこと。
    ("Backward 形で別 index (_id) を基底名で問う",
     "Index Scan Backward using ix_post_not_ok_not_terminal_id on post", False),
]


@pytest.mark.parametrize("label,plan,should_match", _PLAN_FIXTURES,
                         ids=[f[0] for f in _PLAN_FIXTURES])
def test_the_index_pin_matches_every_scan_shape_and_only_the_named_index(
        label, plan, should_match):
    """index の固定は、3 形の scan すべてに効き、かつ別 index には効かないこと。

    この file は同じ族を 3 度踏んでいる:

      1. 素の部分文字列一致 → 基底名が `_id` 版に当たり、**緩すぎた**。
      2. `using <name> on` に絞る → bitmap を取りこぼし、**狭すぎた**。しかもコメントに
         「EXPLAIN は必ず `Index Scan using <name> on <table>` の形で出す」と、実測せずに
         断定して書いていた (trainer-gemini-primary が #136 round 1 で偽と実測)。
      3. どちらの誤りも、DB が要る test の中にあったため **CI では一度も走らなかった**。

    3 を閉じるのがこの test の役目。plan の断片は固定文字列なので DSN が要らず、
    どの CI でも走る。上の 2 つの誤りは、どちらもここで赤くなる。
    """
    hit = bool(_SCAN_USING("ix_post_not_ok_not_terminal").search(plan))
    assert hit is should_match, (
        f"{label}: 一致すべき={should_match} だが実際={hit}。\n  plan: {plan!r}\n"
        "scan node の 3 形 (Index Scan using / Index Only Scan using / Bitmap Index Scan on) "
        "すべてに効き、部分文字列では当たらないこと。")
def _expect_failure_not_skip(call, *, match: str) -> None:
    """`call()` が AssertionError で落ちること。**skip に化けたら、それ自体を失敗にする。**

    この file で同じ罠を **3 度** 踏んだ。検査対象の `_missing_is_a_failure_not_a_skip` は
    条件次第で `pytest.skip` を呼び、その `Skipped` は `BaseException` 派生なので
    `pytest.raises(AssertionError)` を素通りして **その test 自身を SKIP にする**。
    変異試験では「1 failed」ではなく「4 skipped」「5 skipped」として現れ、一覧では
    緑と見分けがつかない。

    1 度目に気づいて直し、2 度目も同じ形で書き、3 度目も書いた。**書き方を憶えておく方式が
    効いていない**ので、正しい形を 1 箇所に閉じ込めて、手で書き直せないようにする。
    skip が故障を隠すことを検出する test が、skip で隠れてはならない。
    """
    try:
        call()
    except AssertionError as exc:
        assert match in str(exc), f"落ちたが理由が違う: {exc}"
        return
    except (KeyboardInterrupt, SystemExit):
        # round 3 (trainer-gemini-primary) F2: `except BaseException` は Ctrl+C と
        # timeout による中断まで握り、**「skip に化けた」という無関係な失敗**に変換して
        # いた。test の中断を test の失敗として報告するのは、報告すべきでないものを
        # 報告している。割り込みはそのまま通す。
        raise
    except BaseException as exc:                                   # noqa: BLE001
        raise AssertionError(
            f"AssertionError を期待したが {type(exc).__name__} が出た ({exc})。"
            "skip に化けている — 一覧では緑と区別がつかない。") from None
    raise AssertionError("失敗も skip もせず、何事もなく通過した。")
class _FakeCursor:
    """`_missing_is_a_failure_not_a_skip` の分岐だけを動かすための最小の cursor。

    `version=None` は **台帳そのものが無い** DB (catalog 引きが 0 行を返す)。
    `fail_with` を渡すと、台帳の読み出しがその例外で失敗する DB を模す — round 2 で
    指摘された「DB 障害が無言 skip に化ける」経路を、実際に踏んで確かめるため。

    複数 head を模せるように、version は **複数** 受け取る。`max(version_num)` を訊かれた
    ときだけ最大を返し、`version_num` をそのまま訊かれたら **物理順の先頭** (= 古い方) を
    返す。これで「`max` を外す」変異が検出できる — 単一行の fake では `max` の有無は
    区別できず、検査したつもりになるだけだった (round 2 の変異試験で気づいた)。
    """

    def __init__(self, *versions: str | None, fail_with: BaseException | None = None):
        self._versions = [v for v in versions if v is not None]
        self._absent = not self._versions
        self._fail_with = fail_with
        self._row: tuple = (None,)

    def execute(self, sql, params=None):
        if "pg_class" in sql:
            # 存在検査は「この接続から見える schema にあるか」。見つかれば schema 名を返す。
            # 失敗を模す場合も「在る」— 読めないだけ、を表す。
            self._row = (None if (self._absent and self._fail_with is None)
                         else ("public",))
            # 台帳の所在は (nspname, on_path) の一覧で返す (round 7 の三分岐)。
            self._rows = ([] if (self._absent and self._fail_with is None)
                          else [("public", 1)])
            return
        if self._fail_with is not None:
            raise self._fail_with
        # 台帳は複数 head を返しうる。物理順のまま返す (呼び出し側が数値で畳む)。
        self._rows = [(v,) for v in self._versions]
        self._row = (self._versions[0],) if self._versions else (None,)

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows
def test_all_indexes_missing_on_an_applied_db_is_a_failure_not_a_skip():
    """0164 適用済みで index 全滅 = 最悪の故障。skip に化けさせない。

    trainer-gemini-primary の Finding 4 を固定する。実測経緯: reviewer の DB は
    `kura_alembic_version` が 0164 でありながら 4 本とも未作成で、旧 `if not rows: skip`
    のもとで **SKIPPED と表示されて無言で通過した**。

    最初に書いた版は `pytest.raises(AssertionError)` だった。**それでは穴を検出できない。**
    穴を戻すと `_missing_is_a_failure_not_a_skip` は `pytest.skip` を呼び、その `Skipped` は
    `BaseException` 派生なので `raises(AssertionError)` を素通りして **この test 自身を
    SKIP にする**。変異試験で「1 failed」ではなく「5 skipped」が出て気づいた。

    つまり私は、skip が故障を隠すことを検出する test を、skip が故障を隠す形で書いていた。
    同じ族をもう一段深く踏んでいる。明示的に型を見る。
    """
    _expect_failure_not_skip(
        lambda: _missing_is_a_failure_not_a_skip(_FakeCursor("0164"), {}),
        match="1 本も無い")
def test_an_unapplied_db_still_skips():
    """逆側: 本当に未適用なら skip のまま。赤くして「DB が無い環境では常に赤」にしない。

    ここも `pytest.raises(Exception)` では書けない (`Skipped` は `BaseException` 派生で、
    この test 自身が skip になり、何も確かめずに緑に見える)。型で受ける。
    """
    for version, label in ((("0163"), "0163 (0164 の手前)"), (None, "台帳ごと無い")):
        try:
            _missing_is_a_failure_not_a_skip(_FakeCursor(version), {})
        except AssertionError as exc:
            raise AssertionError(
                f"{label}: 未適用の DB を赤にしてしまった ({exc})。DB を持たない環境で "
                "常に赤くなる。") from None
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:                               # noqa: BLE001
            assert type(exc).__name__ == "Skipped", f"{label}: 想定外 {type(exc).__name__}"
            assert "未適用" in str(exc)
            continue
        raise AssertionError(f"{label}: skip も失敗もせずに通過した。")
def test_a_ledger_outside_search_path_is_still_found():
    """台帳が search_path の外にあっても見つけること。**AST 検査の置き換え。**

    分離前はこの規則を「発行される SQL の WHERE に `current_schemas` が無いこと」という
    **AST メタ検査**で固定していた。それは「どう書かれているか」の検査で、round 4〜7 に
    3 度「正しい実装を赤くする」を起こした形そのもの。規則は**振る舞い**で固定する。

    この規則は round 5 → 6 → 7 で逆向きに押され続けた末に、reviewer
    (trainer-gemini-primary) が裁定したもの:

      CVE-2018-1058 以降の標準構成 —— アプリ表を `kura` schema に置き
      `search_path = kura, pg_catalog`、台帳は `public` に残る —— では
      **`post` は見えるのに台帳が見えない**。search_path で絞ると、そこで
      「未適用」と誤認して skip し、**index 全滅という最悪の破損が沈黙する**。

    裁定の根拠は**危険度の非対称**だった: 絞る方式は日常的な標準構成で偽 skip を起こし、
    絞らない方式は「本番 schema は未適用なのに backup schema にだけ台帳が残る」という
    特異な汚染 DB でのみ赤くなる。**異常な DB で赤くなる方が、故障を隠して緑で素通りする
    より安全。**
    """
    class _OffPathLedger(_FakeCursor):
        """台帳が 1 つだけ在り、それが search_path の **外** にある DB (on_path=None)。

        **fake は「どの catalog を訊かれたか」で行を返す stub であって、SQL エンジンでは
        ない。** 最初に書いた版は WHERE 句を正規表現で解釈して行数を変えており、
        reviewer (trainer-gemini-primary、round 8) に **自己弁護の完全な再演**だと
        実証された —— 旧 AST 検査の正規表現を `execute()` に移して `assert` を
        `if restricted: return []` に読み替えただけで、**WHERE 句にコメントを 1 行足すと
        偽赤で即死**した。round 4 の「`inspect.getsource` が説明コメントの語に当たって
        自滅した」と構造的に同一。

        境界はこう引く:

          許容   —— どの表・catalog・操作を訊かれたかを見て、**決め打ちの行**を返す
          不許可 —— WHERE の条件式を文字列で解釈し、行集合を動的に増減させる
                    (不完全な SQL parser にしかならず、コメント・空白・別名で偽を量産する)

        「search_path で絞っていないこと」は **SQL の性質**なので、fake では確かめられない。
        それは実 DB に対する `test_the_ledger_is_found_when_search_path_hides_it` が見る。
        ここで確かめるのは **行が返ったときの振る舞い** —— その schema を取り出して
        名指しで読み、skip に倒れないこと。
        """

        def execute(self, sql, params=None):
            if "pg_class" in sql:
                self._rows = [("public", None)]
                self._row = self._rows[0]
                return
            super().execute(sql, params)

    # 見つけた上で「適用済みなのに index 全滅」と判定できること = skip に倒れないこと
    _expect_failure_not_skip(
        lambda: _missing_is_a_failure_not_a_skip(_OffPathLedger("0164"), {}),
        match="1 本も無い")


@pytest.mark.skipif(not _dsn(), reason="kura DB DSN が無い")
def test_the_ledger_is_found_when_search_path_hides_it():
    """search_path が台帳の schema を隠していても、**実 DB で**見つかること。

    この性質は **SQL の性質**なので fake では確かめられない —— fake が返す行は fake が
    決めるからで、そこで WHERE 句を解釈させると「疑似 SQL エンジン」になり、
    コメント 1 つで偽を量産する (round 8 で実証された)。実 PostgreSQL に訊く。

    規則の出どころ (round 5 → 8 の裁定): CVE-2018-1058 以降の標準構成 —— アプリ表を
    `kura` schema に置き `search_path = kura, pg_catalog`、台帳は `public` に残る ——
    では **`post` は見えるのに台帳が見えない**。search_path で絞ると、そこで「未適用」と
    誤認して skip し、**index 全滅という最悪の破損が沈黙する**。

    ここでは search_path を `pg_catalog` だけに絞る (台帳がどの schema にあっても隠れる)。
    それでも「適用済み」と判定できることを見る。
    """
    import psycopg2

    cx = psycopg2.connect(_dsn())
    cx.autocommit = True
    try:
        with cx.cursor() as cur:
            # **素の状態で 0164 に達していることを先に確かめる。** round 8 の確認で
            # reviewer が挙げた偽赤: 0164 未適用の DB (0150 等) では
            # `_index_migration_applied` が素で False を返すので、search_path を隠した
            # あとの assert が「**search_path が隠したせいだ**」という**事実無根の理由**で
            # 落ちる。他の実 DB test は正しく skip に倒れるのに、この 1 本だけが赤くなる。
            # 測ろうとしている性質 (隠しても見つかるか) の**前提**がそもそも無い。
            if not _index_migration_applied(cur):
                pytest.skip(f"{_INDEX_MIGRATION} 未適用の DB — 隠蔽を測る前提がない")

            cur.execute("SELECT count(*) FROM pg_class c"
                        "  JOIN pg_namespace n ON n.oid = c.relnamespace"
                        " WHERE c.relname = 'kura_alembic_version' AND c.relkind IN ('r','p')")
            if cur.fetchone()[0] != 1:
                pytest.skip("この DB の台帳が 1 個ではない — この test の前提ではない")

            cur.execute("SET search_path TO pg_catalog")     # 台帳を search_path から隠す
            assert _index_migration_applied(cur), (
                "search_path が台帳の schema を隠しただけで『未適用』と判定した。"
                "台帳は database ごとに 1 つなので、見えるかどうかで決めてはならない —— "
                "アプリ表が別 schema にある標準構成で、index 全滅が skip に化ける。")
    finally:
        cx.close()


def test_an_unresolvable_ledger_is_not_silently_chosen():
    """台帳が複数あり、どれも search_path に無いなら **落とす**こと。

    黙って 1 つ選ぶのも、黙って skip するのも、どちらも「分からない」を報告していない。
    round 5→7 でこの判定は逆向きに押され続けたので、**判定できない状態そのもの**を
    報告できる形にした。
    """
    class _TwoLedgers(_FakeCursor):
        def execute(self, sql, params=None):
            if "pg_class" in sql:
                self._rows = [("backup", None), ("archive", None)]
                self._row = ("backup",)
                return
            super().execute(sql, params)

    _expect_failure_not_skip(
        lambda: _missing_is_a_failure_not_a_skip(_TwoLedgers("0164"), {}),
        match="どれがこの DB の台帳か判定できない")
def test_an_alphabetic_revision_does_not_hide_a_numeric_one():
    """英字 revision が混じっても、数値 head が 0164 に達していれば適用済みと見ること。

    round 3 F3。`SELECT max(version_num)` は SQL の**文字列**比較なので、Alembic の枝分かれで
    `0164_hotfix` や hex head が入ると辞書順で英字を拾い、数値抽出が失敗して未適用扱いに
    倒れる。全 head を取り、Python 側で数値だけを畳む。
    """
    _expect_failure_not_skip(
        lambda: _missing_is_a_failure_not_a_skip(_FakeCursor("abc123def456", "0164"), {}),
        match="1 本も無い")
def test_an_interrupt_is_not_reported_as_a_test_failure():
    """Ctrl+C / timeout の割り込みは握り潰さず通すこと。

    round 3 F2。`except BaseException` は `KeyboardInterrupt` と `SystemExit` まで捕まえ、
    **「skip に化けている」という無関係な失敗**に変換していた。test の中断を test の失敗として
    報告するのは、報告すべきでないものを報告している。
    """
    def _interrupted():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _expect_failure_not_skip(_interrupted, match="何でもよい")
def test_present_indexes_do_not_trip_the_missing_check():
    """1 本でも見えていれば、この検査は何もしない (全滅のときだけ物を言う)。"""
    _missing_is_a_failure_not_a_skip(_FakeCursor("0164"), {"ix_post_status_null"})
def test_a_broken_ledger_read_is_not_silently_turned_into_a_skip():
    """台帳の読み出しが失敗したら、握り潰さず上へ投げること。

    #136 round 2 (trainer-gemini-primary) Finding 2。元の `except Exception: return False`
    は、台帳の**不在**だけでなく接続断・権限不足・transaction abort・catalog 破損まで
    すべて「未適用」に変換していた。False は skip に倒れるので、**DB 側の障害が丸ごと
    「測れないから skip」に化ける**。この file が直している当の故障の型である。

    不在は catalog 引きで見る (例外ではない)。それ以外は投げる。

    **`pytest.raises(PermissionError)` では書けない。** 穴を戻すと `pytest.skip` の
    `Skipped` (BaseException 派生) が素通りして、この test 自身が SKIP になる。
    変異試験で「1 failed」ではなく「4 skipped」が出た — **同じ罠をこの file で 2 度踏んだ**。
    1 度目で学んだつもりで、新しい test を同じ形で書いていた。型で受ける。
    """
    boom = PermissionError("permission denied for table kura_alembic_version")
    try:
        _missing_is_a_failure_not_a_skip(_FakeCursor("0164", fail_with=boom), {})
    except PermissionError:
        return
    except BaseException as exc:                                   # noqa: BLE001
        raise AssertionError(
            f"台帳の読み出し失敗が {type(exc).__name__} に化けた ({exc})。"
            "握り潰して skip に倒すと、DB 側の障害が「未適用だから測れない」に見える。"
        ) from None
    raise AssertionError("台帳が読めないのに、何事もなく通過した。")
def test_the_ledger_is_read_by_max_not_by_physical_order():
    """複数 head の台帳で、**最大の** revision を見ること。

    #136 round 2 Finding 3。`SELECT version_num` + `fetchone()` は `ORDER BY` が無く、
    物理走査順次第で古い方を拾う。Alembic の枝分かれが起きた DB で、適用済みなのに
    「未適用」と誤判定して skip に倒れる。

    ここでは古い head が先に返る台帳を模す。`max()` を使っていれば 0164 と判定して
    赤 (index 全滅は故障)、物理順の先頭を見ていれば 0150 と判定して skip に倒れる。
    """
    _expect_failure_not_skip(
        # 物理順の先頭は古い方。`max()` を使っていなければ 0150 と判定して skip に倒れる。
        lambda: _missing_is_a_failure_not_a_skip(_FakeCursor("0150", "0164"), {}),
        match="1 本も無い")
