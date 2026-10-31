"""Units that move to nodeb read their database from the node's settings, not a fixed
localhost:5432 (kura-on-nodeb plan 5.2: nodeb's kura is 18/kura on 5433)."""
import importlib

from scripts.kura_dsn import without_password


def test_a_url_dsn_splits_into_an_argv_safe_dsn_and_the_password():
    assert without_password("postgresql+asyncpg://kura:p%40ss@127.0.0.1:5433/kura") == \
        ("host=127.0.0.1 port=5433 user=kura dbname=kura", "p@ss")


def test_reap_authorization_reads_the_database_from_this_nodes_api_env(tmp_path, monkeypatch):
    reap = importlib.import_module("tagger.reap_authorization_eval")
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "env").write_text(
        "KURA_USER=u\nKURA_TOKEN=t\nDATABASE_URL=postgresql+asyncpg://kura:sekret@127.0.0.1:5433/kura\n")
    monkeypatch.setattr(reap, "REPO_ROOT", tmp_path)
    auth, dsn, pw = reap._creds()
    assert dsn == "host=127.0.0.1 port=5433 user=kura dbname=kura" and pw == "sekret"
    assert "sekret" not in dsn
