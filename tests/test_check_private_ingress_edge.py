"""The ingress check reads an nginx edge too, as nginx -T prints it: one upstream to the web
release, tailnet sources only at server level, IPv4 listens, every location to kura_web.
The web/ops template passes once filled in; each way around that is refused."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_private_ingress", ROOT / "scripts/check_private_ingress.py")
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

UPSTREAM = "upstream kura_web { server 127.0.0.1:3003; }\n"
TEMPLATE = (ROOT / "web/ops/edge-nginx.conf").read_text().replace("ADDR4", "192.0.2.10").replace("PORT", "8443")
LIVE = "http://127.0.0.1:3003"


def dump(conf=TEMPLATE, upstream=UPSTREAM):
    return (f"nginx: the configuration file /e/nginx.conf syntax is ok\n# configuration file /e/nginx.conf:\n{conf}\n"
            f"# configuration file /e/upstream.conf:\n{upstream}")


def test_the_template_passes():
    assert check.edge_violations(dump(), LIVE) == []


def test_a_stale_upstream_or_a_direct_backend_is_refused():
    assert check.edge_violations(dump(), "http://127.0.0.1:3002")
    direct = TEMPLATE.replace("    location / {", "    location /api/ { proxy_pass http://127.0.0.1:8001; }\n    location / {")
    assert any("8001" in e for e in check.edge_violations(dump(direct), LIVE))
    served = TEMPLATE.replace("    location / {", "    location /data/ { alias /srv/data/; }\n    location / {")
    assert check.edge_violations(dump(served), LIVE)


def test_an_upstream_defined_in_the_main_file_is_seen():
    # upstream.conf left unused; the real kura_web points at the API
    shadow = TEMPLATE.replace("  include upstream.conf;", "  upstream kura_web { server 127.0.0.1:8001; }")
    assert check.edge_violations(dump(shadow), LIVE)


def test_an_edge_open_beyond_the_tailnet_is_refused():
    assert check.edge_violations(dump(TEMPLATE.replace("    deny all;\n", "")), LIVE)
    assert check.edge_violations(dump(TEMPLATE.replace("allow 100.64.0.0/10;", "allow 0.0.0.0/0;")), LIVE)
    # deny all moved where it guards nothing
    moved = TEMPLATE.replace("    deny all;\n", "").replace("    location / {", "    location /blocked { deny all; proxy_pass http://kura_web; }\n    location / {")
    assert check.edge_violations(dump(moved), LIVE)


def test_an_ipv6_listen_is_refused_quoted_or_not():
    for listen in ('listen [fd7a::1]:8443 ssl;', 'listen "[::]:8443" ssl;', "listen '[::]:8443' quic;",
                   'listen primary.example:8443 ssl;', 'listen 8443 ssl;'):
        v6 = TEMPLATE.replace("    listen 192.0.2.10:8443 ssl;", f"    listen 192.0.2.10:8443 ssl;\n    {listen}")
        assert check.edge_violations(dump(v6), LIVE), listen


def test_unsupported_blocks_and_a_missing_root_location_are_refused():
    inside_if = TEMPLATE.replace("      proxy_pass http://kura_web;", "      proxy_pass http://kura_web;\n      if ($arg_bypass) { proxy_pass http://127.0.0.1:8001; }")
    assert check.edge_violations(dump(inside_if), LIVE)
    start = TEMPLATE.index("    location / {"); end = TEMPLATE.index("    }\n  }\n}")
    no_location = TEMPLATE[:start] + TEMPLATE[end + 6:]
    assert any("location /" in e for e in check.edge_violations(dump(no_location), LIVE))
    static = no_location.replace("  include upstream.conf;", "  include upstream.conf;\n  root /srv/private;")
    assert check.edge_violations(dump(static), LIVE)


def test_text_is_read_as_nginx_reads_it():
    # an escaped '#' is part of the value, so what follows is a live directive, not a comment
    escaped = TEMPLATE.replace("      proxy_pass http://kura_web;", "      proxy_pass http://kura_web;\n      add_header X-Note escaped\\#; if ($arg_bypass) { proxy_pass http://127.0.0.1:8001; }")
    assert check.edge_violations(dump(escaped), LIVE)
    assert check.edge_violations(dump(TEMPLATE + "\n}"), LIVE)               # a stray brace
    assert check.edge_violations(dump(TEMPLATE.rstrip().rstrip("}")), LIVE)   # an unclosed block
