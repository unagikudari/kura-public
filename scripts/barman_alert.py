#!/usr/bin/env python3
"""Tell the owner on Telegram when kura's Barman backup goes wrong (kura-on-nodeb plan, Backups).

Reads what `kura-barman-probe` (every 5 min) and `kura-barman-drill` (monthly) write as the
`barman` user. A status that stopped being written is itself a failure: a dead probe must not
read as a healthy backup. Sends on a change (healthy -> failing, failing -> healthy) and once a
day while it stays failing, with the bot and chat other kura alerts already use (TELEGRAM_BOT_TOKEN /
TELEGRAM_CHAT_ID from api/env).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE = Path(os.environ.get("KURA_BARMAN_STATE", "/var/lib/kura-barman"))
STATUS_MAX_AGE = timedelta(minutes=15)
DRILL_MAX_AGE = timedelta(days=35)
REPEAT = timedelta(days=1)
SENT = Path.home() / ".local/state/kura-barman-alert"


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _age(stamp: str | None, now: datetime) -> timedelta | None:
    try:
        return now - datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None


def reasons(status: dict | None, drill: dict | None, now: datetime) -> list[str]:
    out = []
    age = _age(status.get("at") if status else None, now)
    if age is None:
        out.append("no probe status")
    elif age > STATUS_MAX_AGE:
        out.append(f"probe status is {int(age.total_seconds() // 60)} min old")
    elif not status.get("ok"):
        out.extend(status.get("reasons") or ["probe failed"])
    age = _age(drill.get("finished_at") if drill else None, now)
    if age is None:
        out.append("no restore drill recorded")
    elif not drill.get("ok"):
        out.append(f"restore drill failed: {drill.get('detail')}")
    elif age > DRILL_MAX_AGE:
        out.append(f"last restore drill {age.days} days ago")
    return out


def due(found: list[str], last: dict, now: datetime) -> bool:
    """Whether to send now, given what was last sent."""
    if bool(found) != bool(last.get("failing")):
        return True
    age = _age(last.get("sent_at"), now)
    return bool(found) and (age is None or age >= REPEAT)


def message(server: str, found: list[str]) -> str:
    if not found:
        return f"[kura backup] {server}: OK again"
    return f"[kura backup] {server}: FAILING\n" + "\n".join(f"- {r}" for r in found) + \
        "\nsee docs/notes/kura-on-nodeb-plan.md, Backups on primary's HDD"


class SendFailed(Exception):
    """Telegram was not reached or refused. Carries no URL: the bot token is part of it."""


def send(text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise SendFailed("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set (api/env)")
    body = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body,
                                    timeout=20) as r:
            ok = json.load(r).get("ok") is True
    except urllib.error.HTTPError as e:
        raise SendFailed(f"Telegram answered HTTP {e.code}") from None
    except (OSError, ValueError) as e:
        raise SendFailed(f"Telegram unreachable ({type(e).__name__})") from None
    if not ok:
        raise SendFailed("Telegram refused the message")


def write_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value))
    os.replace(tmp, path)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("server")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    now = datetime.now(timezone.utc)
    found = reasons(_read(STATE / f"{a.server}.status.json"),
                    _read(STATE / f"{a.server}.drill.json"), now)
    sent_path = SENT / f"{a.server}.json"
    last = _read(sent_path) or {}
    if a.dry_run:
        print(json.dumps({"reasons": found, "send": due(found, last, now),
                          "message": message(a.server, found)}, ensure_ascii=False, indent=1))
        return 0
    # Exit 0: healthy. 1: failing, and the owner has been told. 2: the owner could not be told
    # (the unit fails, so systemd shows it too).
    if due(found, last, now):
        try:
            send(message(a.server, found))
        except SendFailed as e:
            print(f"[barman-alert] {a.server}: not sent: {e}", file=sys.stderr)
            return 2
        try:
            write_atomic(sent_path, {"failing": bool(found), "sent_at": now.isoformat()})
        except OSError as e:
            print(f"[barman-alert] {a.server}: sent, but not recorded ({type(e).__name__})",
                  file=sys.stderr)
            return 2
    return 1 if found else 0


def run(argv=None) -> int:
    """Any unexpected error is exit 2, never 1 (which the unit treats as "failing, owner told").
    Only the exception's type is printed: a message could carry the bot token's URL."""
    try:
        return main(argv)
    except Exception as e:  # noqa: BLE001 — the point is that nothing escapes as exit 1
        print(f"[barman-alert] unexpected {type(e).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(run())
