"""Posts that must not be used for future learning (training, GT, review and
evaluation sets): posts in a learning-excluded pool, the image-level opt-out tag, and -- owner age-check ruling,
rule 5 (~/.local/state/review/owner-rulings/age-check.md) -- every post carrying
a youth tag. Exclusion means "never chosen as a teacher or evaluation image";
kura's posts and tags are not changed (rule 4)."""

OPT_OUT_TAG = "tagger-training-exclude"
# Rule 5's list, plus kura tags whose names state the same (stricter, never looser).
YOUTH_TAGS = ("loli", "shota", "child", "female_child", "male_child", "aged_down", "toddler",
              "oppai_loli", "kindergarten_uniform", "randoseru", "baby")
YOUTH_NAMED_TAGS = ("onee-shota", "onii-shota", "lolidom", "toddlercon", "onee-loli", "child_on_child")
EXCLUDING_TAGS = (OPT_OUT_TAG,) + YOUTH_TAGS + YOUTH_NAMED_TAGS

# Any name of the tag (aliases included); names are fixed literals above, never input.
# A post in a learning-excluded pool (tagger_auto_exclude_pool, the pool page's
# 学習除外 switch: the owner's own generations, photos...) is excluded the same
# way -- that switch used to stop only auto-tagging, so such posts still reached
# review, GT and training (owner 2026-11-26).
POST_NOT_EXCLUDED = """
(NOT EXISTS (
    SELECT 1 FROM post_tag ex
    JOIN tag_name name ON name.tag_id=ex.tag_id
    WHERE ex.post_id={post_id} AND name.name IN (%s)
) AND NOT EXISTS (
    SELECT 1 FROM pool_post ex_pp
    JOIN tagger_auto_exclude_pool ex_pool ON ex_pool.pool_id=ex_pp.pool_id
    WHERE ex_pp.post_id={post_id} AND ex_pool.excluded
))
""" % ", ".join("'%s'" % name for name in EXCLUDING_TAGS)


def not_excluded(post_id):
    return POST_NOT_EXCLUDED.format(post_id=post_id)


# Answers given on these review purposes measure a model; they must never
# become its teachers (evaluation independence). An answer on a schema-/2 set
# still sets a human pin, and the pin->GT sync turns it into human_correction
# GT, so GT read for training drops labels whose current pin came from one.
# boundary_recheck doubles as a fixed evaluation set (kura#64 review).
# (dedicated_candidate is a provenance kind of blind /3 sets, which never pin.)
EVALUATION_PURPOSES = ("candidate_evaluation", "production_precision", "fixed_evaluation",
                       "base_comparison", "boundary_recheck")

EVALUATION_AUDITS = """
SELECT e.audit_id FROM tagger_human_review_event e
WHERE e.audit_id IS NOT NULL AND e.source_context #>> '{queue,purpose}' IN (%s)
""" % ", ".join("'%s'" % purpose for purpose in EVALUATION_PURPOSES)


def not_evaluation_answer(gt):
    """SQL: the GT row aliased ``gt`` did not come from an evaluation answer."""
    return (f"NOT EXISTS (SELECT 1 FROM post_tag_pin ev_pin WHERE ev_pin.post_id={gt}.post_id "
            f"AND ev_pin.tag_id={gt}.tag_id AND ev_pin.audit_id IN ({EVALUATION_AUDITS}))")


def training_label(gt):
    """SQL: the GT row aliased ``gt`` may teach: its post is not excluded and
    the label is not an evaluation answer."""
    return not_excluded(f"{gt}.post_id") + " AND " + not_evaluation_answer(gt)
