"""tag merge が identity (帰属リンク・別名・人手 pin) を運ぶことを固定する。

■ 2026-09-04 の実害 (artist タグ重複統合、user 依頼の表記ゆれ掃除)

merge-into は post_tag しか運ばず、source の tag_link (作家帰属の SoT —
_derive_artist_tags が読む)・別名・post_tag_pin (人手 GT)・actor claim を
ORM cascade で黙って削除していた。artist:mokimoki → mokimoki の統合で帰属
導線が消える形。さらに source の名前が消えると、取り込み時の display-name 由来
タグが**同じ重複を再製造する** (名前が別名として残れば既存タグへ解決する)。

DB を見ない source 走査 (CI で DB 無しに走る) — 適用結果の検証は実 DB での
統合作業そのものが担った (2026-09-04、37 ペア)。
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "api" / "routers" / "tags.py").read_text()
BODY = SRC[SRC.index("async def merge_tags"):SRC.index("# ── Tag external links")]


def test_merge_moves_the_attribution_links():
    assert "UPDATE tag_link SET tag_id" in BODY, (
        "merge が tag_link を運ばない — 統合した瞬間に作家帰属 (_derive_artist_tags) が消える")


def test_merge_keeps_source_names_as_aliases():
    assert "UPDATE tag_name tn SET tag_id" in BODY, (
        "merge が source の名前を別名として残さない — 取り込み時の display-name 由来タグが"
        "同じ重複を再製造する")


def test_merge_moves_human_pins():
    assert "UPDATE post_tag_pin SET tag_id" in BODY, "人手 pin (GT) が merge で消える"


def test_merge_does_not_orm_delete_the_source():
    assert "db.expunge(source)" in BODY and "await db.delete(source)" not in BODY, (
        "ORM delete は cascade が (今は target の別名になった) names を消しに行く")
