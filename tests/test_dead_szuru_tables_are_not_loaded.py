"""szurubooru 由来の死表を「毎ページ読む」形に戻さないための固定 (kura#41)。

■ 何が起きていたか (2026-09-02 監査 + 2026-09-03 実測)

`comment` / `comment_score` / `post_score` / `post_feature` / `post_note` は
szurubooru から引き継いだ表で、**production で実測 0 行**、かつ書き手が
1 つも無い (唯一の書き手だった `api/routers/comments.py` は kura#41 で削除)。

にもかかわらず `_serialize_post` が `commentCount` / `noteCount` /
`featureCount` / `score` を relationship の長さから算出していたため、post を
返す全経路 (`/api/posts` の 1 ページ含む) が `selectinload` を 4 本ぶら下げて
いた。**恒久的に空と分かっている表への、保証された無駄 SELECT** が 11 箇所
× 4 本。szuru 互換 JSON の contract は保ちたいので、フィールドは残して値を
0 定数にし、eager-load を落とした (`favorited_by` は 286 行の実データがある
ので残す)。

■ ここで見るもの

1. 死 relationship を eager-load していないこと
2. 死 relationship を読んでいない (= 0 定数が非同期 lazy-load に戻らない)
3. **書き手が居ないこと** — 0 定数が正しいのは「表が空のまま」だからで、
   その根拠は「INSERT する経路が無い」以外にない。書き手が生えたら
   commentCount=0 は嘘になるので、そのときここが落ちる。

DB を見ない (CI で DB 無しに走る)。行数そのものの監視ではなく、行が生える
**経路**を止める。
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
API = REPO / "api"

# szurubooru 由来・実測 0 行・書き手なしの relationship (Post 側の属性名)
DEAD_RELATIONSHIPS = ("comments", "notes", "features", "scores")
# 同じく死表の ORM class — 生成 (INSERT) が現れたら 0 定数が嘘になる
DEAD_MODELS = ("Comment", "CommentScore", "PostScore", "PostFeature",
               "PostNote", "TagSuggestion")


def _api_sources():
    """api/ 配下の python source (ORM 定義そのものと alembic を除く)。"""
    for path in sorted(API.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith("api/alembic/") or rel == "api/db/models.py":
            continue
        yield rel, path.read_text()


def test_dead_relationships_are_not_eager_loaded():
    bad = [(rel, rel_name)
           for rel, src in _api_sources()
           for rel_name in DEAD_RELATIONSHIPS
           if f"selectinload(Post.{rel_name})" in src]
    assert not bad, f"恒久的に空の表を eager-load している: {bad}"


def test_dead_relationships_are_not_read():
    """`len(post.comments)` の類が戻ると eager-load も一緒に戻る (async lazy
    load は MissingGreenlet になるため)。読まない形を固定する。"""
    bad = [(rel, rel_name)
           for rel, src in _api_sources()
           for rel_name in DEAD_RELATIONSHIPS
           if f"post.{rel_name}" in src]
    assert not bad, f"恒久的に空の表を読んでいる: {bad}"


def test_no_writer_creates_rows_in_the_dead_tables():
    """0 定数の根拠は「書く経路が無い」こと。生成が現れたらここで止める。"""
    bad = [(rel, model)
           for rel, src in _api_sources()
           for model in DEAD_MODELS
           if f"{model}(" in src]
    assert not bad, (
        f"死表に行を作る経路が生えた: {bad} — "
        "commentCount/noteCount/featureCount/score の 0 定数 (api/routers/posts.py "
        "_serialize_post) が嘘になる。復活させるなら定数と eager-load を戻すこと")


def test_the_dead_comments_router_stays_deleted():
    """`api/routers/comments.py` (呼び手ゼロ / comment 表の唯一の書き手)。"""
    assert not (API / "routers" / "comments.py").exists()
    assert "comments" not in (API / "main.py").read_text()
