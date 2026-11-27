"""Private PostgreSQL: a candidate base's vocabulary maps onto kura tags; unknown names are held."""
import asyncio
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

sys.path.insert(0, str(Path(__file__).parent))
from test_human_review_live import pg, url  # noqa: F401,E402

from tagger import base_model, base_vocabulary  # noqa: E402


def test_unknown_names_are_held_unless_the_owner_approved_them(url):
    async def go():
        engine = create_async_engine(url)
        try:
            async with async_sessionmaker(engine)() as db:
                rows = [{"name": "fixture", "category": 0}, {"name": "Fixture", "category": 0},
                        {"name": "brand_new_tag", "category": 0},
                        {"name": "owner_ok_tag", "category": 4},
                        {"name": "general", "category": base_vocabulary.RATING_CATEGORY}]
                report = await base_vocabulary.map_vocabulary(db, rows, approved={"owner_ok_tag"})
                assert (report["total"], report["mapped"]) == (3, 1)
                assert report["unmapped_held"] == [{"name": "brand_new_tag", "category": 0}]
                assert report["approved_additions"] == [{"name": "owner_ok_tag", "category": 4}]
                assert report["shared_tags"] == {}
                again = await base_vocabulary.map_vocabulary(db, rows, approved={"owner_ok_tag"})
                assert again == report
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_descriptors_and_approvals_file_are_consistent(tmp_path):
    descriptors = base_model.load()
    assert descriptors["active"] == "wd-swinv2-tagger-v3"
    assert "wd-eva02-large-tagger-v3" in descriptors["models"]
    assert base_vocabulary.approvals_for("wd-eva02-large-tagger-v3") == set()
    csv_path = tmp_path / "selected_tags.csv"
    csv_path.write_text("tag_id,name,category,count\n1,a_b,0,5\n2,safe,9,7\n")
    assert base_vocabulary.read_vocabulary(csv_path) == [
        {"name": "a_b", "category": 0}, {"name": "safe", "category": 9}]
