"""nodec_autotag_worker reads its credentials from its own environment (tagger/env).

On nodeb, ~/kura/api/env became the API's own settings on 2026-10-25 and lost
KURA_USER, and the worker, which read only that file, crash-looped until 10/29.
"""
import base64

import pytest

from tagger import nodec_autotag_worker as w


def _user(headers):
    return base64.b64decode(headers["Authorization"].split()[1]).decode().split(":")[0]


def test_the_environment_wins_over_the_api_env_file(tmp_path, monkeypatch):
    envf = tmp_path / "env"
    envf.write_text("KURA_DB_ROLE=rehearsal\nKURA_USER=from-file\nKURA_TOKEN=t-file\n")
    monkeypatch.setattr(w, "ENVF", str(envf))
    monkeypatch.setenv("KURA_USER", "from-env")
    monkeypatch.setenv("KURA_TOKEN", "t-env")
    assert _user(w.auth_header()[0]) == "from-env"


def test_an_api_env_without_credentials_is_not_a_crash(tmp_path, monkeypatch):
    envf = tmp_path / "env"
    envf.write_text("KURA_DB_ROLE=rehearsal\n")
    monkeypatch.setattr(w, "ENVF", str(envf))
    monkeypatch.setenv("KURA_USER", "tagger")
    monkeypatch.setenv("KURA_TOKEN", "t")
    assert _user(w.auth_header()[0]) == "tagger"


def test_the_file_still_serves_a_node_whose_environment_has_none(tmp_path, monkeypatch):
    envf = tmp_path / "env"
    envf.write_text("KURA_USER=from-file\nKURA_TOKEN=t-file\n")
    monkeypatch.setattr(w, "ENVF", str(envf))
    monkeypatch.delenv("KURA_USER", raising=False)
    monkeypatch.delenv("KURA_TOKEN", raising=False)
    assert _user(w.auth_header()[0]) == "from-file"


def test_no_credentials_anywhere_still_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "ENVF", str(tmp_path / "absent"))
    monkeypatch.delenv("KURA_USER", raising=False)
    monkeypatch.delenv("KURA_TOKEN", raising=False)
    with pytest.raises(ValueError, match="KURA_USER and KURA_TOKEN"):
        w.auth_header()
