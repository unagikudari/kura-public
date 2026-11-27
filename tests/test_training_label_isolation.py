"""Evaluation answers never become teachers: every GT read for training goes
through post_exclusion.training_label(), which drops labels whose pin came
from an evaluation-purpose answer, on top of the post exclusion."""
from pathlib import Path

from api.tagger_core import post_exclusion as pe

REPO = Path(__file__).resolve().parent.parent
READERS = ("api/tagger_core/label_identity.py", "tagger/remote_learning.py", "tagger/vnext_arms.py",
           "tagger/dedicated_protocol.py", "api/tagger_core/exclusive_training.py",
           "tagger/train_base_finetune.py", "tagger/vnext_runner.py")


def test_training_label_combines_post_exclusion_and_evaluation_isolation():
    sql = pe.training_label("g")
    assert pe.not_excluded("g.post_id") in sql
    assert "ev_pin.post_id=g.post_id AND ev_pin.tag_id=g.tag_id" in sql
    for purpose in ("candidate_evaluation", "production_precision", "fixed_evaluation",
                    "base_comparison", "boundary_recheck"):
        assert f"'{purpose}'" in sql
    assert "%" not in sql and ":" not in sql.replace("::", "")  # safe in psycopg2 and text()


def test_every_training_gt_reader_uses_it():
    for path in READERS:
        assert 'training_label("g")' in (REPO / path).read_text() or "training_label('g')" in (REPO / path).read_text(), path


def test_heads_use_the_same_evaluation_list():
    from tagger.heads import canonical_gt
    assert canonical_gt.EVALUATION_AUDITS_SQL is pe.EVALUATION_AUDITS


def test_vnext_recovered_positives_skip_evaluation_pins():
    text = (REPO / "tagger/vnext_arms.py").read_text()
    assert "ev_pin.audit_id in ({EVALUATION_AUDITS})" in text
    assert "and %s=any(a.added_tags)" in text and "(tag, tag))" in text


def test_exclusive_pairs_read_training_labels_only():
    text = (REPO / "api/tagger_core/exclusive_training.py").read_text()
    assert text.count("training_label('g')") == 2  # paired_evidence and missing_counterpart
