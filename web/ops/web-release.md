# Warm web releases

The web bundle runs from an immutable, user-owned release directory, not the
checkout being built. A deployment starts the unused loopback slot (3002/3003),
checks login, the precise asset version and anonymous API refusal, then updates
only this origin's existing `/`, `/api`, `/data/` and optional `/m` Serve routes.
Both processes stay alive during the route updates; other host/port handlers are
untouched. A failed readiness check leaves ingress unchanged. Failed public
checks restore the captured routes without killing either process.

Install `kura-web-release@.service` from this directory into the user's systemd
unit directory and reload systemd once. With a clean committed checkout:

```
npm --prefix web run build
python3 web/scripts/web-release.py --source "$PWD" --origin https://<canonical-host>:8443
```

Node must be on PATH or supplied with `--node /absolute/path/to/node`. Existing
private `web/.env.server` is read by systemd, never copied into the bundle or
passed as argv. Per-release env files hold only ports, runtime paths and origin.
Only the selected active unit remains enabled for reboot. The previous process
is not stopped during switching; when reusing its slot the controller refuses
to proceed if any established connections remain. Files are never overwritten,
and the current plus three previous successful generations' own hashed assets
are available from the new server. Release folders are retained for inspection;
there is no destructive automatic filesystem cleanup.

`~/.local/share/kura-web/active.json` is the committed deployment receipt.
Private-ingress validation follows its port, and checkout polling compares its
commit instead of treating an in-place build as a deployed frontend. The old
`kura-web.service` is disabled after the initial switch and connection drain.
Do not use `restart kura-web` to ship a new frontend after this transition.

API and DB migrations are separate. This controller does not restart the API,
run migrations or certify schema compatibility between backend releases.
