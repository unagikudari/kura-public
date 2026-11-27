"""Dashboard first-screen observation, refreshed independently of requests."""
from pathlib import Path
import math
import time

from sqlalchemy import text

from api.services.dashboard_base_comparison import read_json

STATE = Path.home() / ".local/state/kura"
CACHE = STATE / "dashboard-runtime.json"
TEMPERATURES = STATE / "node-temperatures.json"


def fresh_snapshot(path=None, *, now=None):
    now = time.time() if now is None else now
    row = read_json(CACHE if path is None else path)
    if not isinstance(row, dict) or row.get("schema") != "kura-dashboard-runtime/1":
        return {"state": "unavailable"}
    at = row.get("observed_at")
    if type(at) not in (int, float) or not math.isfinite(at) or not -5 <= now - at <= 90:
        return {"state": "unavailable"}
    return row


def maximum_temperature(path=None, *, now=None, fallback=None):
    now = time.time() if now is None else now
    payload = read_json(TEMPERATURES if path is None else path)
    observations = (payload.get("nodes") if isinstance(payload, dict)
                    and payload.get("schema") == "kura-node-temperature-observations/1" else None)
    if not isinstance(observations, list):
        observations = []
    if not observations and fallback:
        observations = [fallback]
    valid = []
    for row in observations:
        if not isinstance(row, dict):
            continue
        host, celsius, at = (row.get(key) for key in ("host", "celsius", "observed_at"))
        if (not isinstance(host, str) or not host or len(host) > 63
                or type(celsius) not in (float, int) or not 0 <= celsius <= 130
                or type(at) not in (float, int) or not -5 <= now - at <= 150):
            continue
        valid.append((celsius, host, at, row.get("level")))
    if not valid:
        return None
    celsius, host, at, level = max(valid, key=lambda row: row[:3])
    result = dict(host=host, celsius=celsius, observed_at=at)
    if level in ("normal", "warn", "crit"):
        result["level"] = level
    return result


async def capture(db, *, temperature_fallback=None):
    release = (await db.execute(text(
        "SELECT release_id,generation FROM tagger_release_active WHERE id=1"
    ))).mappings().one_or_none()
    claimed = await db.scalar(text("""
        SELECT count(*) FROM tagger_lifecycle_lock
        WHERE scope LIKE 'lifecycle:%' AND expires_at > clock_timestamp()
    """))
    now = time.time()
    return dict(schema="kura-dashboard-runtime/1", state="available", observed_at=now,
                learning={"leased": claimed, "source": "lifecycle_lock"},
                release=dict(release) if release else None,
                maximum_temperature=maximum_temperature(now=now, fallback=temperature_fallback))
