#!/usr/bin/env bash
# One-shot post-reboot reporter for the 2026-07-31 NVIDIA driver/library mismatch.
# Runs verify_nvidia_after_reboot.sh once the machine is back up, then posts the
# output to broker task 25fca0d7 (the reboot itself kills the agent session that
# scheduled this, so the completion report has to be delivered from this side).
set -uo pipefail

TASK_ID="25fca0d7"
BROKER_URL="http://primary:8000/mcp/"
BROKER_KEY="d54fb5ff75134e7aba3ba058b6d2af8060174482f5f44b3cb84ad67744954e56"
LOG="$HOME/tagger-heads/post_reboot_verify.log"

# Wait for postgres to accept connections (logical replication needs it up before
# pg_stat_subscription is meaningful), then give the subscriber time to reconnect.
for _ in $(seq 1 30); do
    pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1 && break
    sleep 10
done
sleep 60

{
    echo "post-reboot verify @ $(date -Is)"
    echo "kernel: $(uname -r)   uptime: $(uptime -p)"
    echo
    bash "$HOME/tagger-heads/verify_nvidia_after_reboot.sh"
} >"$LOG" 2>&1

BROKER_URL="$BROKER_URL" BROKER_KEY="$BROKER_KEY" TASK_ID="$TASK_ID" \
python3 - "$LOG" <<'PY'
import json, os, sys, time, urllib.request

log = open(sys.argv[1], encoding="utf-8", errors="replace").read()
body = log if len(log) <= 14000 else log[:14000] + "\n...(truncated)"

# Pass = kernel module version == userspace, nvidia-smi answered, torch sees the GPU.
ok = "MATCH:" in log and "Failed to initialize NVML" not in log and "True" in log

note = (
    ("再起動完了・検証 PASS" if ok else "再起動完了・検証 FAIL (要確認)")
    + " — kernel 7.0.0-27 → 28、post-boot 自動実行 (reboot でエージェントセッションが落ちるため"
    " post-reboot-verify.service から投函)。\n\n```\n" + body + "\n```\n\n"
    "再発防止 (root 案件・未実施): trainer で `sudo apt-mark hold "
    "libnvidia-compute-580 nvidia-driver-580-open nvidia-dkms-580-open nvidia-utils-580 "
    "nvidia-compute-utils-580` — unattended-upgrade が走行中カーネルモジュールを追い越すのを止める。"
)


def call(name, args):
    req = urllib.request.Request(
        os.environ["BROKER_URL"],
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": name, "arguments": args}}).encode(),
        headers={"X-API-Key": os.environ["BROKER_KEY"],
                 "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
    )
    return urllib.request.urlopen(req, timeout=60).read().decode()


for attempt in range(5):
    try:
        print(call("manage_task", {"task_id": os.environ["TASK_ID"], "action": "complete",
                                   "note": note, "source_agent_id": "trainer-cc-primary"}))
        if not ok:
            call("report_signal", {"kind": "friction", "key": "trainer-post-reboot-gpu",
                                   "urgency": 3,
                                   "payload_json": json.dumps({"log": sys.argv[1]})})
        break
    except Exception as exc:  # broker or DNS not up yet
        print(f"attempt {attempt}: {exc}", file=sys.stderr)
        time.sleep(30)
PY
