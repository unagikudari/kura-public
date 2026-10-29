# Security

## Reporting a vulnerability

Please report it privately through GitHub's
[private vulnerability reporting](https://github.com/unagikudari/kura-public/security/advisories/new)
for this repository, not in a public issue. Include what you did, what happened, and the
version (`/api/build` or the `VERSION` file).

You will get an answer there. Once a fix is released, the advisory is published with it
(and a CVE requested through GitHub), crediting you if you wish.

## How fast

Counted from when we learn of it until a release carries the fix:

| Severity | Fix released within |
|---|---|
| Critical | 3 days |
| High | 14 days |
| Medium | 30 days |
| Low | the next release |

The dependencies an install receives (`api/requirements.lock` and every web front end
dependency, since the framework is compiled into the server) are watched through GitHub's
Dependabot alerts on this repository and audited again against the public advisory
databases before every release; a release is held while a Critical or High one is open.

## Supported versions

Only the latest release receives fixes. Update with the steps in the README's
"Upgrading" section.

## What kura expects of its deployment

- The API listens on `127.0.0.1:8001` and the web server on `127.0.0.1:3000`. Do not publish
  either port; serve only your reverse proxy's HTTPS origin.
- `api/env` and `web/.env.server` hold the services' secrets (`ops/install/make_env.py` writes
  them 0600), and the administrator's sign-in key is in the file `create_admin.py --output`
  named (`~/kura-admin.key` in the README). Keep all three out of reach of others, including
  in backups; delete the key file once the key is stored somewhere safe.
- Every request that changes something needs a signed-in user or the machine key; reads on
  the API port itself are the internal interface, which is why that port stays on loopback.
