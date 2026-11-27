import pytest

from tagger import base_frames
from tagger.dedicated_protocol import digest


def _scores(n=40, tags=("t",)):
    inc = {str(i): (0.9 if i % 2 else 0.1) for i in range(1, n + 1)}
    cand = {str(i): (0.9 if i % 3 else 0.1) for i in range(1, n + 1)}
    body = {"schema": "tagger-base-scores/2", "tags": list(tags), "skipped": [], "population_sha256": "p" * 64,
            "models": {"incumbent": {"model_sha256": "a" * 64, "scores": {t: inc for t in tags}},
                       "candidate": {"model_sha256": "b" * 64, "scores": {t: cand for t in tags}}}}
    return dict(body, sha256=digest(body))


def _media(n=40, missing=()):
    return {"sha256": "m" * 64, "media": [
        {"id": i, "status": "ok", "type": "image", "exclusion": None, "blob_sha256": f"{i:064x}"}
        for i in range(1, n + 1) if i not in missing]}


def test_youth_posts_are_removed_from_every_cell_and_recorded():
    doc = base_frames.build(_scores(), _media(), {"youth": {2, 3}},
                            thresholds={"incumbent": 0.35, "candidate": 0.35}, per_cell=5)
    frame = doc["frames"]["t"]
    assert sum(c["population"] for c in frame["cells"].values()) == 38
    assert all(i not in (2, 3) for c in frame["cells"].values() for i in c["sample"])
    assert doc["excluded_post_ids"] == {"youth": [2, 3], "no_display_image": []}
    assert set(doc["youth_tags"]) >= {"loli", "shota", "child", "onee-shota"}
    sampled = {i for c in frame["cells"].values() for i in c["sample"]}
    assert set(doc["display_hashes"]) == {str(i) for i in sampled}


def test_frames_are_reproducible_from_the_scores():
    kwargs = dict(thresholds={"incumbent": 0.35, "candidate": 0.35}, per_cell=5)
    assert base_frames.build(_scores(tags=("t", "u")), _media(), {}, **kwargs) == \
        base_frames.build(_scores(tags=("t", "u")), _media(), {}, **kwargs)


def test_posts_without_a_display_image_are_removed_symmetrically():
    doc = base_frames.build(_scores(), _media(missing=(5, 6)), {},
                            thresholds={"incumbent": 0.35, "candidate": 0.35}, per_cell=50)
    assert doc["excluded_post_ids"]["no_display_image"] == [5, 6]
    assert sum(c["population"] for c in doc["frames"]["t"]["cells"].values()) == 38


def test_document_round_trips_and_hashes_come_back_keyed_by_int(tmp_path):
    from tagger.dedicated_protocol import read_verified, write_once

    doc = base_frames.build(_scores(tags=("t", "u")), _media(), {"youth": {2}},
                            thresholds={"incumbent": 0.35, "candidate": 0.35}, per_cell=5)
    write_once(tmp_path / "frames.json", doc)
    back = read_verified(tmp_path / "frames.json", base_frames.SCHEMA)
    assert digest(back["frames"]) == digest(doc["frames"])
    hashes = base_frames.sampled_display_hashes(back)
    sampled = {i for f in back["frames"].values() for c in f["cells"].values() for i in c["sample"]}
    assert sampled and all(i in hashes for i in sampled)


def test_scored_bytes_must_match_the_display_blob(tmp_path):
    import hashlib

    doc = {"display_hashes": {"1": hashlib.sha256(b"one").hexdigest()}}
    (tmp_path / "1.bin").write_bytes(b"one")
    base_frames.verify_scored_bytes(tmp_path, doc)
    (tmp_path / "1.bin").write_bytes(b"changed")
    with pytest.raises(Exception, match="scored_bytes_mismatch"):
        base_frames.verify_scored_bytes(tmp_path, doc)


def test_wrong_scores_schema_is_refused():
    scores = dict(_scores(), schema="tagger-base-scores/1")
    with pytest.raises(Exception, match="scores_schema_invalid"):
        base_frames.build(scores, _media(), {}, thresholds={"incumbent": 0.35, "candidate": 0.35})


def test_youth_query_passes_rule_5_and_named_variants():
    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params):
            self.params = params

        def fetchall(self):
            return [(7,)]

    cur = Cur()

    class Conn:
        def cursor(self):
            return cur

    assert base_frames.youth_posts(Conn(), {9, 7}) == {7}
    assert cur.params[0] == [7, 9] and {"female_child", "onee-shota", "toddlercon"} <= set(cur.params[1])


def test_frames_and_training_share_one_exclusion_list():
    from api.tagger_core import post_exclusion

    assert base_frames.YOUTH_TAGS is post_exclusion.YOUTH_TAGS
    sql = post_exclusion.not_excluded("p.id")
    for name in post_exclusion.YOUTH_TAGS + post_exclusion.YOUTH_NAMED_TAGS + (post_exclusion.OPT_OUT_TAG,):
        assert f"'{name}'" in sql
