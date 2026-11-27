#!/usr/bin/env python3
# Description: rotate the kura-edge access log and summarise how long requests took
# Effects: `rotate` renames logs/access.log, makes nginx reopen it, writes a report next to
#          the archive, gzips it and removes archives older than --keep-days; `report` only reads
"""Request timing for the kura edge (web/ops/edge-nginx.conf, log_format `kura_timed`).

The edge writes one tab-separated line per request:

    time  status  method  path  request_time  upstream_response_time  bytes  protocol  client  user_agent

`path` is nginx's `$uri`: the query string is not logged. `request_time` runs from the first
byte read from the client to the last byte sent to it, so it includes the client's network;
`upstream_response_time` is the origin's share. A browser request is one whose user agent
says Mozilla; everything else (taggers, import workers, scripts) is a worker.

    edge-log.py report FILE [--json]      summarise one log (plain or .gz)
    edge-log.py rotate [--keep-days 14]   rotate, report, compress, prune (the daily timer)

Reports land in ~/.local/state/kura-edge/reports/<archive>.json, plus latest.json.
Lines in any other format (logs written before kura_timed) are counted as `unparsed`.
"""
from __future__ import annotations

import argparse
import fcntl
import gzip
import json
import math
import os
import re
import signal
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

EDGE = Path(os.environ.get("KURA_EDGE_DIR", Path.home() / ".local/share/kura-edge"))
STATE = Path(os.environ.get("KURA_EDGE_STATE", Path.home() / ".local/state/kura-edge"))
FIELDS = 10
NUMERIC = re.compile(r"^\d+$")
HEXLIKE = re.compile(r"^[0-9a-f]{16,}$", re.I)
OPAQUE = re.compile(r"^(?=.*\d)[A-Za-z0-9_-]{24,}$")
FILEISH = re.compile(r"^[^/]*\.(?:webp|jpe?g|png|gif|avif|webm|mp4|mkv|zip|cbz|json|js|css|svg|woff2?|ico)$", re.I)


def route(path: str) -> str:
    """Collapse ids, hashes and file names so that one page or endpoint is one route."""
    parts = path.split("?", 1)[0].split("/")
    out: list[str] = []
    for i, seg in enumerate(parts):
        if i and parts[1:2] == ["data"] and i >= 3:
            out.append("*")
            break
        if NUMERIC.match(seg):
            out.append(":n")
        elif HEXLIKE.match(seg) or OPAQUE.match(seg):
            out.append(":h")
        elif FILEISH.match(seg) and i == len(parts) - 1 and i > 1:
            out.append(":file")
        else:
            out.append(seg)
    return "/".join(out) or "/"


def seconds(text: str) -> float | None:
    # upstream_response_time may list several attempts ("0.010, 0.020") or be "-"
    total, seen = 0.0, False
    for piece in text.replace(":", ",").split(","):
        piece = piece.strip()
        if piece and piece != "-":
            try:
                total += float(piece)
                seen = True
            except ValueError:
                return None
    return total if seen else None


def pct(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return math.nan
    k = max(0, min(len(sorted_values) - 1, math.ceil(q * len(sorted_values)) - 1))
    return sorted_values[k]


def opener(path: Path):
    return gzip.open(path, "rt", errors="replace") if path.suffix == ".gz" else open(path, errors="replace")


def summarise(path: Path) -> dict:
    groups: dict[tuple[str, str], dict] = defaultdict(lambda: {"t": [], "up": [], "bytes": 0, "5xx": 0})
    unparsed = 0
    first = last = None
    with opener(path) as fh:
        for line in fh:
            cols = line.rstrip("\n").split("\t")
            if len(cols) != FIELDS:
                unparsed += 1
                continue
            when, status, method, uri, rt, urt, size, _proto, _client, agent = cols
            t = seconds(rt)
            if t is None:
                unparsed += 1
                continue
            first = first or when
            last = when
            who = "browser" if "Mozilla" in agent else "worker"
            g = groups[(who, f"{method} {route(uri)}")]
            g["t"].append(t)
            up = seconds(urt)
            if up is not None:
                g["up"].append(up)
            g["bytes"] += int(size) if size.isdigit() else 0
            if status.startswith("5"):
                g["5xx"] += 1
    routes = []
    for (who, name), g in groups.items():
        t = sorted(g["t"])
        up = sorted(g["up"])
        routes.append({
            "client": who, "route": name, "count": len(t),
            "p50_ms": round(pct(t, .5) * 1000), "p95_ms": round(pct(t, .95) * 1000),
            "p99_ms": round(pct(t, .99) * 1000), "max_ms": round(t[-1] * 1000),
            "origin_p95_ms": round(pct(up, .95) * 1000) if up else None,
            "total_s": round(sum(t), 1), "mb": round(g["bytes"] / 1e6, 1), "errors_5xx": g["5xx"],
        })
    routes.sort(key=lambda r: -r["total_s"])
    return {"file": str(path), "from": first, "to": last, "unparsed_lines": unparsed,
            "requests": sum(r["count"] for r in routes), "routes": routes}


def text(report: dict, top: int = 15, min_count: int = 20) -> str:
    out = [f"{report['file']}  {report['from']} → {report['to']}  requests {report['requests']}"
           f"  unparsed {report['unparsed_lines']}"]
    for who in ("browser", "worker"):
        rows = [r for r in report["routes"] if r["client"] == who and r["count"] >= min_count]
        if not rows:
            continue
        out.append(f"\n{who}: slowest p95 (routes with ≥{min_count} requests)")
        out.append(f"{'p50':>7} {'p95':>7} {'p99':>7} {'origin95':>8} {'count':>7} {'5xx':>4}  route")
        for r in sorted(rows, key=lambda r: -r["p95_ms"])[:top]:
            o = "-" if r["origin_p95_ms"] is None else r["origin_p95_ms"]
            out.append(f"{r['p50_ms']:>7} {r['p95_ms']:>7} {r['p99_ms']:>7} {o:>8} {r['count']:>7} {r['errors_5xx']:>4}  {r['route']}")
    out.append("\nms; p95 includes the client's network, origin95 is the web/API's own share.")
    return "\n".join(out)


def master_pid() -> int:
    return int((EDGE / "tmp/nginx.pid").read_text().strip())


def nginx_pids(master: int) -> list[int]:
    """The master and its children (the workers), read from /proc. A process that exits while
    being read is skipped; any other unreadable entry raises (we cannot tell who writes)."""
    if not Path(f"/proc/{master}").exists():
        raise ProcessLookupError(f"nginx master {master} is not running")
    pids = [master]
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            continue                 # exited meanwhile
        except ProcessLookupError:
            continue
        if int(fields[1]) == master:
            pids.append(int(stat.parent.name))
    return pids


def holders(path: Path, pids: list[int]) -> list[int]:
    """Processes among `pids` that still have `path`'s inode open. A process that has exited
    holds nothing; one whose descriptors cannot be read (permission, anything else) raises,
    because "could not look" must never count as "released"."""
    st = path.stat()
    held = []
    for pid in pids:
        try:
            fds = list(Path(f"/proc/{pid}/fd").iterdir())
        except (FileNotFoundError, ProcessLookupError):
            continue                 # exited: holds nothing
        for fd in fds:
            try:
                o = os.stat(fd)
            except (FileNotFoundError, ProcessLookupError):
                continue             # descriptor closed meanwhile
            if (o.st_dev, o.st_ino) == (st.st_dev, st.st_ino):
                held.append(pid)
                break
    return held


def released(path: Path, timeout: float) -> list[int]:
    """Wait until no nginx process writes to `path`; return the ones still holding it."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            held = holders(path, nginx_pids(master_pid()))
        except (OSError, ValueError) as e:  # PermissionError, a missing master, a bad pid file
            print(f"cannot tell who writes {path.name}: {e}", file=sys.stderr)
            held = [-1]              # treat as still held
        if not held or time.monotonic() >= deadline:
            return held
        time.sleep(0.2)


def unique(directory: Path, stem: str, suffix: str) -> Path:
    for n in range(1000):
        p = directory / f"{stem}{'' if n == 0 else f'-{n}'}{suffix}"
        if not p.exists() and not Path(f"{p}.gz").exists():
            return p
    raise RuntimeError(f"no free archive name for {stem}")


def write_atomic(path: Path, body: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as fh:
        fh.write(body)
    os.replace(tmp, path)


def finish(moved: Path, reports: Path) -> dict:
    """Report, then gzip through a temporary name; the plain archive goes only after the .gz exists."""
    report = summarise(moved)
    body = json.dumps(report, ensure_ascii=False, indent=1) + "\n"
    write_atomic(reports / f"{moved.stem}.json", body)
    write_atomic(reports / "latest.json", body)
    fd, tmp = tempfile.mkstemp(dir=moved.parent, prefix=f".{moved.name}.", suffix=".gz.tmp")
    os.close(fd)
    with open(moved, "rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
        while chunk := src.read(1 << 20):
            dst.write(chunk)
    os.replace(tmp, f"{moved}.gz")
    moved.unlink()
    return report


def rotate(keep_days: int, timeout: float = 30.0) -> int:
    """Exit 0 when every archive is compressed; 1 when one is left plain because nginx still
    writes to it (it is collected by the next run); 75 when another rotate holds the lock."""
    log = EDGE / "logs/access.log"
    archive = EDGE / "logs/archive"
    archive.mkdir(exist_ok=True)
    reports = STATE / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    lock = open(STATE / "rotate.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another rotate is running", file=sys.stderr)
        return 75
    if log.exists() and log.stat().st_size > 0:
        moved = unique(archive, f"access-{datetime.now().strftime('%Y%m%d-%H%M%S')}", ".log")
        os.rename(log, moved)
        os.kill(master_pid(), signal.SIGUSR1)   # nginx: reopen log files
    status = 0
    last = None
    # This run's archive and any an earlier run had to leave plain, oldest first.
    for plain in sorted(archive.glob("access-*.log")):
        held = released(plain, timeout)
        if held:
            print(f"kept {plain.name} uncompressed: still open in nginx pid(s) {held}", file=sys.stderr)
            status = 1
            continue
        last = finish(plain, reports)
    cutoff = time.time() - keep_days * 86400
    for old in list(archive.glob("access-*.log.gz")) + list(reports.glob("access-*.json")):
        if old.stat().st_mtime < cutoff:
            old.unlink()
    if last:
        print(text(last))
    elif status == 0:
        print("nothing to rotate")
    return status


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report")
    r.add_argument("file", type=Path)
    r.add_argument("--json", action="store_true")
    r.add_argument("--min-count", type=int, default=20)
    ro = sub.add_parser("rotate")
    ro.add_argument("--keep-days", type=int, default=14)
    a = ap.parse_args(argv)
    if a.cmd == "report":
        rep = summarise(a.file)
        print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json else text(rep, min_count=a.min_count))
        return 0
    return rotate(a.keep_days)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
