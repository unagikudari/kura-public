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

## nginx edge instead of Serve

Tailscale Serve terminates TLS with HTTP/2 only. On 2026-10-31 thumbnails behind it
stalled for 15–20 s at a time on a desktop Chrome, and the same release behind nginx
with HTTP/3 did not stall. To use nginx for an origin, take the origin off Serve
(`tailscale serve --https=<port> off`) and run `edge-nginx.conf` from
`~/.local/share/kura-edge/` as a user service. `--edge` names another directory.
The controller then rewrites `upstream.conf` there and reloads nginx in place of the
Serve routes, and restores it when the public checks fail. An origin both on Serve
and with an edge directory is refused. The certificate comes from
`tailscale cert --cert-file certs/host.crt --key-file certs/host.key <host>`; renew it
before it expires, as Serve did on its own. `scripts/check_private_ingress.py` checks the
edge as well: every proxy_pass goes to the web release, tailnet sources only, no IPv6
listener.

`~/.local/share/kura-web/active.json` is the committed deployment receipt.
Private-ingress validation follows its port, and checkout polling compares its
commit instead of treating an in-place build as a deployed frontend. The old
`kura-web.service` is disabled after the initial switch and connection drain.
Do not use `restart kura-web` to ship a new frontend after this transition.

API and DB migrations are separate. This controller does not restart the API,
run migrations or certify schema compatibility between backend releases.
