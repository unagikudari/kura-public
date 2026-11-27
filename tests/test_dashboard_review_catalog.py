from datetime import datetime, timezone
import json

from api.services.dashboard_review_catalog import project
from api.services.dashboard_runtime_cache import fresh_snapshot, maximum_temperature


def test_pending_rows_hide_evidence_and_order_work():
    def row(key, kind, *, purpose="historical_label_recheck", schema=2, lane="lifecycle", cohort=None, answered=0):
        return dict(queue_sha256=key * 64, tag=key, purpose=purpose, schema_version=schema,
                    review_kind=kind, lane=lane, cohort_kind=cohort,
                    created_at=datetime(2026, 11, 27, tzinfo=timezone.utc), total=20, answered=answered)

    source = [
        row("a", "findings", lane="lora_findings"),
        row("b", "blind_comparison", purpose="base_comparison", schema=3),
        row("c", "training_support", purpose="training_support"),
        row("d", "age_check"),
        row("e", "prepublication", purpose="production_precision"),
        row("f", "blind_comparison", purpose="base_comparison", schema=3, cohort="base_switch"),
        row("g", "recheck", answered=20),
    ]
    pending, completed = project(source)
    assert [item["tag"] for item in pending] == list("fedcba")
    assert [item["label"] for item in pending] == ["乗り換え比較", "公開前確認", "年齢確認", "正例集め", "盲検", "誤り候補"]
    assert completed == [dict(tag="g", purpose="historical_label_recheck", sets=1)]
    assert all(set(item) == {"tag", "purpose", "label", "lane", "total", "remaining", "href"} for item in pending)


def test_runtime_counts_and_maximum_use_only_fresh_observations(tmp_path):
    path = tmp_path / "temperatures.json"
    path.write_text(json.dumps(dict(schema="kura-node-temperature-observations/1", nodes=[
        dict(host="primary", observed_at=995, celsius=86),
        dict(host="nodeb", observed_at=1000, celsius=70),
        dict(host="trainer", observed_at=800, celsius=100),
    ])))
    assert maximum_temperature(path, now=1000) == dict(host="primary", observed_at=995, celsius=86)
    assert maximum_temperature(path, now=2000) is None
    path.write_text(json.dumps(dict(schema="kura-dashboard-runtime/1", state="available", observed_at=1000)))
    assert fresh_snapshot(path, now=1005)["state"] == "available"
    assert fresh_snapshot(path, now=1091) == {"state": "unavailable"}


def test_trainer_csv_uses_package_temperature_and_preserves_the_observation_time():
    from scripts.dashboard_thermal_sources import thermal_csv

    csv = """time,cpu_tctl_c,cpu_tccd1_c
2026-11-27T07:55:15+0900,86.2,40.1
2026-11-27T07:55:30+0900,84.1,41.2
2026-11-27T07:55:45+0900,,42.1
"""
    row = thermal_csv(csv, "trainer")
    assert row == dict(host="trainer", celsius=84.1,
                       observed_at=datetime.fromisoformat("2026-11-27T07:55:30+0900").timestamp())
