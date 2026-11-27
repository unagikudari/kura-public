import asyncio
import json

from api.services import dashboard_base_comparison as comparison
from api.services.human_review import digest


def test_progress_and_verdict_are_bound_to_the_registered_answers(tmp_path, monkeypatch):
    refs = {f"tag-{i}": f"{i:064x}" for i in range(1, 21)}
    (tmp_path / "eva02-r3-references.json").write_text(json.dumps(refs))

    class Registry:
        def all(self):
            return list(refs.items())

        async def execute(self, stmt, params):
            assert "base_comparison" in str(stmt)
            assert set(params["refs"]) == set(refs.values())
            return self

    decisions = ["present", "uncertain"]

    async def answers(db, ref):
        return dict(answers=[{"decision": value} for value in decisions], sha256=ref)

    monkeypatch.setattr(comparison, "evaluation_answers", answers)
    def get():
        return asyncio.run(comparison.read_comparison(Registry(), tmp_path))

    assert (get()["answered"], get()["total"], get()["verdict"]) == (20, 40, None)
    decisions[1] = "absent"
    assert get()["complete"] and get()["verdict"] is None
    body = dict(
        schema="tagger-base-switch-verdict/1", answers_sha256=refs,
        verdict=dict(admissible=False, reasons=["priority_tag_regression"]),
        results=[dict(tag=tag, complete=True, significant_regression=True, difference=dict(f1=-.02)) for tag in refs],
    )
    path = tmp_path / "eva02-r3-verdict.json"
    path.write_text(json.dumps(dict(body, sha256=digest(body))))
    assert get()["verdict"]["tags"]["tag-1"] == dict(f1_change=-.02, regression=True)
    body["answers_sha256"] = dict(refs, **{"tag-1": "0" * 64})
    path.write_text(json.dumps(dict(body, sha256=digest(body))))
    assert get()["verdict"] is None
