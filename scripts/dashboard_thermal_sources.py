"""Normalize temperature telemetry before it reaches the dashboard cache."""
import csv
from datetime import datetime
import subprocess


def thermal_csv(text, host):
    """Read the package temperature and its own timestamp from the logger."""
    readings = []
    for row in csv.DictReader(text.splitlines()):
        try:
            observed_at = datetime.fromisoformat(row["time"])
            celsius = float(row["cpu_tctl_c"])
        except (KeyError, TypeError, ValueError):
            continue
        if observed_at.tzinfo is not None and 0 <= celsius <= 130:
            readings.append(dict(host=host, celsius=celsius, observed_at=observed_at.timestamp()))
    return max(readings, key=lambda row: row["observed_at"], default=None)


def read_trainer_csv():
    """A bounded background fallback until the fleet's JSON is installed."""
    try:
        command = (
            'p="$HOME/.local/state/thermal-log/$(date +%F).csv"; '
            'head -n 1 "$p" && tail -n 10 "$p"'
        )
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2",
             "-o", "ClearAllForwardings=yes", "trainer", command],
            capture_output=True, text=True, timeout=3, check=True,
        )
        return thermal_csv(result.stdout, "trainer")
    except (OSError, subprocess.SubprocessError):
        return None
