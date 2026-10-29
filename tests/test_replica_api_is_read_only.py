"""replica 宣言ノードの kura-api が書き込みを機構的に拒むことを固定する。

■ 2026-09-04 の実害 (trainer-cc-primary の照合 ac05ace8 で発見)

「trainer の kura 書き込みは primary:8443 へ (local 直書き禁止)」は規約でしか
なく、local API は普通に書けた。拡張のワーカーの KURA_URL 既定
127.0.0.1:8001 (= ノードごとに対象が変わる禁止則違反) が trainer の local
API へ書き、拡張の表がローカル採番 (seq 100M 帯) → 複製置換 → 拡張の監査/
ファイル表に**孤児 594 行**。規約は破られたときに音がしない — 機構は 403 で鳴る。

固定するのは:
  - KURA_DB_ROLE=replica のとき mutating メソッドが middleware で拒まれる
    (新しい設定は増やさない — replica 宣言は trainer の api/env に既在)。
  - 127.0.0.1 既定が書き込みスクリプトから消えていること (再発の種) — こちらは
    拡張の script を読むので、拡張の test に分けた (2026-10-25, G6)。
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_replica_role_gates_a_read_only_middleware():
    src = (REPO / "api" / "main.py").read_text()
    assert '_DB_ROLE in _READ_ONLY_ROLES' in src and "_replica_read_only" in src and '_READ_ONLY_ROLES = ("replica", "rehearsal")' in src, (
        "replica read-only middleware が消えた — local API への書き込みが再び無音で通る")
    body = src[src.index("_replica_read_only"):]
    assert '("GET", "HEAD", "OPTIONS")' in body[:600], (
        "読み系メソッドの許可リストが変わった — replica local API の存在理由は読みの提供")


def test_sequence_heal_does_not_run_on_a_replica():
    """setval() は書き込みで、lifespan は read-only middleware を通らない。

    2026-09-04 の敵対的レビューで判明 (trainer の live sequence 値から実証):
    論理レプリケーションは subscriber の sequence を進めないので replica では
    `last_value < max(id)` が恒常的に真になり、この heal が起動のたびに canonical
    の採番点まで各 sequence を引き上げていた。それは「local の誤挿入は local で
    衝突する」という分離を消し、次の local insert が primary の次の id を取って、
    複製 INSERT が duplicate key で apply worker を全 56 表ぶん停止させる
    (2026-07-30 の停止クラス)。ガードは呼び出し側ではなく関数の根に置く。
    """
    src = (REPO / "api" / "main.py").read_text()
    body = src[src.index("async def _heal_stale_sequences"):]
    guard = body[:body.index("async_session")]
    assert '_DB_ROLE in _READ_ONLY_ROLES' in guard and "return" in guard, (
        "replica ガードが _heal_stale_sequences の根から消えた — "
        "replica の sequence が canonical の採番点まで進み、次の local insert が "
        "複製 apply を停止させる")


def test_an_unknown_db_role_refuses_to_boot_instead_of_falling_back_to_canonical():
    """typo 1 つで保護 3 枚 (read-only / migration 抑止 / sequence 分離) が同時に消える。

    KURA_DB_ROLE=replika のような綴り違いは、以前は黙って canonical 扱いになった。
    replica 上でそれが起きると unattended DDL が subscriber に走る。役割の宣言は
    綴りに依存してよい判断ではないので、未知の値は起動を止める (未設定は canonical)。
    """
    src = (REPO / "api" / "main.py").read_text()
    head = src[src.index("_DB_ROLE = "):src.index("_MIGRATION_LOCK_KEY")]
    assert "_DB_ROLES" in head and "raise RuntimeError" in head, (
        "未知の KURA_DB_ROLE が canonical に倒れる状態に戻った")
    assert "fix:" in head, "エラーに次の 1 action が無い (裸の error code は禁止)"
