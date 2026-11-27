# Backup status receipt

The signed-in `/settings/backup` page reads only a bounded, local JSON receipt.
It does not connect to SSH, inspect deployment environment files, hash media,
start backups or run a restore from a request. Unknown, unavailable and stale
(over one hour) observations never receive a healthy indicator. A verified
receipt describes its particular snapshot, not coverage of later writes.

`web/scripts/library-backup-status.py` reads the latest timestamped snapshot
under `trainer:/data/kura-recovery`, compares the recovery manifest to the
clean-reopen restore proof, and atomically publishes mode-0600 status at
`~/.local/state/kura-library-backup/status.json`. The remote probe requires the
independent `/data` mount. Only counts, times, locations and proof state leave
the probe; source/config archives and credentials are never page data. A failed
probe retains the last known snapshot but marks it unavailable. This is metadata
observation, not a fresh checksum pass or a new restore verification.

Install both files from this directory into `~/.config/systemd/user/`, then run
`systemctl --user daemon-reload` and
`systemctl --user enable --now kura-library-backup-status.timer`.
Run `systemctl --user start kura-library-backup-status.service` for the initial
observation. The timer observes every fifteen minutes. The library-wide backup
itself remains manual; the existing collector-only backup timer has a different
scope. UI fixture tests can set the server-only `KURA_BACKUP_STATUS_FILE` to an
isolated receipt file. Never point it at a full recovery manifest or env file.

# Edge request timing

The edge (`edge-nginx.conf`) logs every request with `log_format kura_timed`: time, status,
method, path without the query string, `request_time`, `upstream_response_time`, bytes,
protocol, client address and user agent. `request_time` includes the client's network;
`upstream_response_time` is the web/API's own share.

`kura-edge-log.timer` runs `web/scripts/edge-log.py rotate` every night at 04:10: the log is
renamed into `logs/archive/`, nginx reopens it (SIGUSR1), a report is written to
`~/.local/state/kura-edge/reports/` (`latest.json` is the newest) and the archive is gzipped.
Archives and reports older than 14 days are removed. Read a report with
`python3 web/scripts/edge-log.py report <archive.log.gz>`; the browser table is what people
wait for, the worker table is taggers and import workers.

Install both units from this directory into `~/.config/systemd/user/`, then
`systemctl --user daemon-reload && systemctl --user enable --now kura-edge-log.timer`.
