"""Map a candidate base model's vocabulary onto kura tags (base-switch plan step 2).

Owner ruling 2026-11-23: names kura has no tag for are *held* and listed, never
auto-created; only names the owner lists in base_vocabulary_approvals.json may
become new tags. Matching follows api/services/tags.get_or_create_tags: the
lowercased name against every tag_name (aliases included). Rating rows
(category 9) are handled by the rating lane, not as tags.
"""
import argparse
import asyncio
import csv
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import text

APPROVALS = Path(__file__).resolve().parent / "base_vocabulary_approvals.json"
RATING_CATEGORY = 9


def read_vocabulary(path):
    with open(path, newline="") as source:
        return [{"name": row["name"], "category": int(row["category"])}
                for row in csv.DictReader(source)]


def approvals_for(model_name, path=APPROVALS):
    data = json.loads(Path(path).read_text())
    return set(data.get("approved_additions", {}).get(model_name, []))


async def map_vocabulary(db, rows, *, approved=frozenset()):
    """``unmapped_held`` means "no kura tag; not created" -- unrelated to quality holds."""
    categories = {}
    for row in rows:
        if row["category"] != RATING_CATEGORY:
            categories.setdefault(row["name"].lower(), row["category"])
    names = sorted(categories)
    found = dict((await db.execute(text("""
        SELECT tn.name, tn.tag_id FROM tag_name tn WHERE tn.name = ANY(:names)
    """), {"names": names})).all()) if names else {}
    by_tag = {}
    for name, tag_id in found.items():
        by_tag.setdefault(tag_id, []).append(name)
    unmapped = [name for name in names if name not in found]
    return {
        "schema": "tagger-base-vocabulary/2",
        "total": len(names),
        "mapped": len(found),
        "unmapped_held": [{"name": name, "category": categories[name]}
                          for name in unmapped if name not in approved],
        "approved_additions": [{"name": name, "category": categories[name]}
                               for name in unmapped if name in approved],
        # Several vocabulary names landing on one kura tag (e.g. via an alias).
        "shared_tags": {str(tag_id): sorted(names_) for tag_id, names_ in sorted(by_tag.items())
                        if len(names_) > 1},
        "mapping_sha256": hashlib.sha256(json.dumps(
            sorted(found.items()), separators=(",", ":")).encode()).hexdigest(),
    }


async def _main(args):
    from tagger import base_model
    from api.db.session import async_session

    descriptors = base_model.load()["models"]
    if args.model not in descriptors:
        raise SystemExit(f"unknown base model {args.model!r}")
    model = dict(descriptors[args.model], name=args.model)
    if args.csv:
        local = args.csv
    else:
        from huggingface_hub import hf_hub_download
        local = hf_hub_download(model["repo"], "selected_tags.csv", revision=model["revision"])
    csv_path = base_model.verify_file(model, "selected_tags.csv", local)
    async with async_session() as db:
        report = await map_vocabulary(db, read_vocabulary(csv_path),
                                      approved=approvals_for(args.model))
    report.update(model=args.model, repo=model["repo"], revision=model["revision"])
    out = (Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
           / "kura-tagger" / "base-vocabulary" / f"{args.model}@{model['revision'][:12]}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps({key: report[key] for key in ("model", "total", "mapped")}
                     | {"unmapped_held": len(report["unmapped_held"]), "report": str(out)}))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True, help="descriptor name in tagger/base_models.json")
    parser.add_argument("--csv", help="a local copy of the tag list (still verified against the pin)")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())


async def priority_tags(db, rows, *, top_usage=10, top_corrections=10):
    """Owner ruling 2026-11-23: the top tags by usage plus the top tags by human
    corrections, among the base vocabulary's general tags. Tags whose output a
    content-head policy decides are left out: switching the base does not change
    them."""
    general = {row["name"].lower() for row in rows if row["category"] == 0}
    heads = set((await db.execute(text("SELECT tag FROM tagger_current_head_policy"))).scalars().all())
    usage = (await db.execute(text("""
        SELECT tn.name FROM tag t JOIN tag_name tn ON tn.tag_id = t.id AND tn.ord = 0
        ORDER BY t.post_count DESC NULLS LAST, t.id LIMIT 2000
    """))).scalars().all()
    corrected = (await db.execute(text("""
        SELECT tag FROM tagger_gt_label WHERE derived_from = 'human_correction'
        GROUP BY tag ORDER BY count(*) DESC, tag LIMIT 2000
    """))).scalars().all()
    eligible = [name for name in usage if name in general and name not in heads][:top_usage]
    eligible += [name for name in corrected
                 if name in general and name not in heads and name not in eligible][:top_corrections]
    return {"tags": eligible, "excluded_head_policy_tags": sorted(heads & general)}
