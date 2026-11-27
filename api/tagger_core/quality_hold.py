"""Machine-only abstentions after a measured quality incident.

This is NOT a negative label or reap authority. Human editing is unaffected.
Remove a hold only after a replacement passes a frozen positive/negative test.
"""

# 2026-11-15: active head 860f7b68... scores all 32 recent human-negative
# examples above 0.85. Legacy extraction preprocessing still scores 25/32
# positive. Convenience sample, not a prevalence estimate or calibration set.
# See docs/notes/smegma-quality-incident-20261115.md.
# 2026-11-17: frozen production sample, 197/200 owner-negative (3 positive).
# Owner explicitly labelled the remainder negative after selecting the 3;
# this is not 197 individual button responses. New machine assertions must
# stop at auto_tag/pool_meta completion, without falling back to an older head.
# See docs/notes/dark-labia-quality-incident-20261117.md.
# 2026-11-17: all 5 positives among a fixed 128-image unjudged sample
# were owner-negative; canonical absent pins verified. Owner approved hold.
# This small sample is not a population error-rate estimate.
# See docs/notes/dark-penis-quality-incident-20261117.md.
# 2026-11-17: owner judged the 7 positive predictions in a fixed 128-image
# sample: 3 correct / 4 wrong; wrong predictions include the highest score.
# Owner approved minora hold; false assertions cost more than abstentions.
# See docs/notes/minora-quality-incident-20261117.md.
MACHINE_QUALITY_HOLDS = frozenset({
    "smegma", "dark_labia_majora", "dark_penis", "dark_labia_minora",
})


async def held_names(db) -> frozenset[str]:
    """Canonical database decisions apply at every machine completion.

    Existing workers may still emit a quarantined tag. No worker rollout, cache
    TTL, or restart is required for this write-side boundary. Query failure is
    a failed completion, never permission to make an assertion. The incident
    floor remains until the replacement's deployment is independently qualified.
    """
    from sqlalchemy import text
    names = (await db.execute(text("SELECT tag FROM tag_quarantine WHERE active"))).scalars()
    return MACHINE_QUALITY_HOLDS | frozenset(names)
