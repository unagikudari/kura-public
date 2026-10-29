# kura

A self-hosted image archive with an auto-tagger: posts, pools, tags and search over your own
library. The API is FastAPI on PostgreSQL; the web front end is SvelteKit.

## Requirements

- Linux with systemd (tested on Ubuntu 26.04)
- PostgreSQL 18 with `pgvector` (`pg_trgm` and `fuzzystrmatch` ship with PostgreSQL itself),
  and `sudo` to run `psql` as `postgres`
- Python 3.14, Node.js 22.23.4, 24.22.1, 26.12.1 or newer (Node's October 2026 security
  releases; `npm ci` refuses a lower version number, including a distribution build that
  backports fixes), `ffmpeg`, `git`

On Ubuntu 26.04 Node.js comes from its snap, which tracks Node's own releases (the
distribution's `nodejs` package stays on 22.22.1), and the rest from the distribution:

```bash
sudo apt update
sudo apt install git postgresql-18 postgresql-18-pgvector python3-venv ffmpeg
sudo snap install node --classic --channel=24/stable
```

## Install

```bash
git clone https://github.com/unagikudari/kura-public.git ~/kura && cd ~/kura

# 1. The API's Python environment
python3 -m venv ~/.local/share/kura-api/venv
~/.local/share/kura-api/venv/bin/pip install -r api/requirements.lock

# 2. Settings: writes api/env and web/.env.server (0600) with every secret generated.
#    --origin is the address people will open kura at.
python3 ops/install/make_env.py --origin https://kura.example.org --data-dir ~/kura-data

# 3. The database: roles, database, extensions and the schema (asks for sudo).
~/.local/share/kura-api/venv/bin/python ops/install/database.py create

# 4. The first administrator. The sign-in key goes to the file you name, never to the screen.
~/.local/share/kura-api/venv/bin/python scripts/create_admin.py create \
    --name admin --email you@example.org --output ~/kura-admin.key

# 5. The web front end. Its origin is fixed at build time from ORIGIN in web/.env.server,
#    so to change it later, edit that line and build again.
(cd web && npm ci && npm run build)

# 6. Start both
mkdir -p ~/.config/systemd/user
cp systemd/user/kura-api.service systemd/user/kura-web.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kura-api kura-web
sudo loginctl enable-linger "$USER"   # keep them running after you log out, and start them at boot
```

The web server listens on `127.0.0.1:3000`. Serve your `--origin` from it through a reverse
proxy that terminates HTTPS; sign-in only works when the browser comes in on exactly that
origin. With nginx, for example:

```nginx
server {
    listen 443 ssl;
    server_name kura.example.org;
    ssl_certificate     /etc/ssl/kura/fullchain.pem;
    ssl_certificate_key /etc/ssl/kura/privkey.pem;
    client_max_body_size 512m;

    location / {
        proxy_pass         http://127.0.0.1:3000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_read_timeout 300s;
    }
}
```

Open your origin and sign in with your email and the key in `~/kura-admin.key`. Keep that file
private; delete it once you have stored the key somewhere safe.

## Install with Docker

Instead of the steps above, with Docker and its Compose plugin:

```bash
git clone https://github.com/unagikudari/kura-public.git ~/kura && cd ~/kura
python3 ops/install/make_env.py --docker --origin http://localhost:3000
docker compose up -d --build
docker compose run --rm tools python scripts/create_admin.py create \
    --name admin --email you@example.org --output /out/kura-admin.key
```

Open http://localhost:3000 and sign in with your email and the key in `~/kura/kura-admin.key`.
The database, the images and the settings stay in Docker volumes and in `api/env`,
`web/.env.server` and `.env` (all 0600).

The web listens on 127.0.0.1:3000 only. To serve it to other machines, give `make_env.py` the
address people will open (`--origin https://kura.example.org`) and put an HTTPS reverse proxy
(the nginx block above works) in front of port 3000; `KURA_BIND` and `KURA_PORT` in `.env`
change where it listens.

To add the auto-tagger (see "Auto-tagging" below for what it does):

```bash
docker compose --profile tagger build tagger
docker compose --profile tagger run --rm tagger python tagger/build_release.py --base-only --output-dir /models/release
docker compose run --rm tools python tagger/publish_release.py --build-dir /models/release
docker compose run --rm tools python scripts/tagger_worker_setup.py \
    --user admin --output /out/tagger/env --url http://api:8001 --host kura-tagger
docker compose --profile tagger up -d
```

To upgrade: `docker compose --profile tagger down`, `git pull`, then
`docker compose --profile tagger up -d --build` (leave out `--profile tagger` if you did not add it).
The schema upgrade runs before the API starts.

## Language

The interface is in English or Japanese. It follows your browser's language until you pick one
under Menu → Language. To set the default for everyone who has not picked one, put
`KURA_UI_LOCALE=en` or `KURA_UI_LOCALE=ja` in `web/.env.server`.

## Adding images

Drag image or video files, or whole folders, onto the kura page, or paste a copied image
(Ctrl+V). Each one is in the list as soon as it has been added.

## Auto-tagging (optional)

A worker tags new images with the WD14 tagger (`SmilingWolf/wd-swinv2-tagger-v3`, about 400 MB,
downloaded once from Hugging Face). It runs on the CPU; a GPU only makes it faster. Run these
once, from `~/kura`:

```bash
# 1. The tagger's Python environment. torch and torchvision come from the same index — the CPU
#    build here; for an NVIDIA GPU use the CUDA index that pytorch.org names instead.
python3 -m venv ~/.local/share/kura-tagger/venv
~/.local/share/kura-tagger/venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
~/.local/share/kura-tagger/venv/bin/pip install -r tagger/requirements.txt

# 2. The first tagger release: the base model, published to the database.
~/.local/share/kura-tagger/venv/bin/python tagger/build_release.py --base-only --output-dir ~/kura-data/tagger-release
(set -a; . api/env; set +a; ~/.local/share/kura-api/venv/bin/python tagger/publish_release.py --build-dir ~/kura-data/tagger-release)

# 3. The worker's sign-in: a token for your user and this machine on the worker list,
#    written to tagger/env (0600). Use the name you gave create_admin.py.
~/.local/share/kura-api/venv/bin/python scripts/tagger_worker_setup.py --user admin --output tagger/env

# 4. Start it
cp systemd/user/kura-tagger-worker.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kura-tagger-worker
```

`journalctl --user -u kura-tagger-worker` shows each image it tags. Images added before the
worker started are tagged too.

## Upgrading

An install that took Node.js from the distribution's `nodejs` package (as this README said
before 1.290.6) switches to the snap once: run `sudo apt remove nodejs npm && sudo snap install
node --classic --channel=24/stable` first, and after `git pull` below also
`cp systemd/user/kura-web.service ~/.config/systemd/user/ && systemctl --user daemon-reload`
(the old unit started `/usr/bin/node`).

```bash
cd ~/kura
systemctl --user stop kura-api kura-web
git pull
~/.local/share/kura-api/venv/bin/pip install -r api/requirements.lock
~/.local/share/kura-api/venv/bin/python ops/install/database.py upgrade
(cd web && npm ci && npm run build)
systemctl --user start kura-api kura-web
```

`database.py upgrade` is needed because some schema changes replace objects owned by a
separate database role; the API's own role is never given that privilege
(`docs/notes/fresh-install-baseline.md`).

## A lost sign-in key

```bash
~/.local/share/kura-api/venv/bin/python scripts/create_admin.py recover --user-id 1 --output ~/kura-admin.key
```

The old key stops working and a new one is written to the file.

## License

kura is free software under the GNU Affero General Public License v3.0 — see [LICENSE](LICENSE).
The menu links to this repository; if you run a modified kura for other people over a network,
set `KURA_SOURCE_URL` in `web/.env.server` to where your modified source can be had.
