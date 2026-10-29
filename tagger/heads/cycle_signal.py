#!/usr/bin/env python3
# Description: head 更新サイクルの結果を broker signal にする (失敗を可視化)。
# Kind: reporter
# Effects: writes one signal on the local broker
"""サイクルの結果を broker から見えるようにする。

**なぜ要るか**: 2026-08-02〜03 に本サイクルは 5 回連続で失敗し、タガーの再学習が
2 日間止まった。しかし気づけなかった — journal に出るのは gate の `WORKER_EXIT
rc=1` だけで、実際のエラー (CUDA OOM) は `cycle.log` に埋もれる。update_cycle.sh
は先頭で `exec >> cycle.log` しており、**stdout/stderr が systemd に届かない**。
失敗が黙るなら監視は存在しないのと同じ。

成功時も upsert する。urgency が 1 → 3 に下がることで「直った」が読め、
立てっぱなしの警報 (やがて無視される) にならない。

使い方: cycle_signal.py <urgency> <detail...>
何があっても exit 0 — 報告の失敗が本体を殺してはいけない。
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

# The broker key is sourced from the broker checkout, not copied here -- one place
# to rotate. Path is configuration so a clean checkout can point elsewhere.
ENV = Path(os.environ.get("BROKER_ENV_FILE",
                          os.path.expanduser("~/deploy-checkout/.env")))
BROKER = "http://localhost:8000"


def main() -> int:
    if len(sys.argv) < 3:
        return 0
    urgency, detail = int(sys.argv[1]), " ".join(sys.argv[2:])
    try:
        m = re.search(r"^API_KEY_CC_TRAINER=(.+)$", ENV.read_text(), re.M)
        if not m:
            return 0
        req = urllib.request.Request(
            BROKER + "/signals/upsert", method="POST",
            data=json.dumps({
                "kind": "tagger-head-update", "signal_key": "trainer",
                "urgency": urgency,
                # detail は「次の 1 手」まで含める。「失敗した」だけの警報は
                # 読んだ人が何もできず、結局放置される。
                "payload": {"detail": detail[:400], "host": "trainer",
                            "log": "trainer:~/tagger-heads/cycle.log"},
            }).encode(),
            headers={"X-API-Key": m.group(1).strip(),
                     "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # noqa: BLE001 — 報告の失敗で本体を殺さない
        print(f"[cycle-signal] 報告できなかった: {type(e).__name__}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
