"""Replay the installed baseline through the real asyncpg Alembic chain."""
import os
from pathlib import Path
import subprocess
import sys
import pytest
sys.path.insert(0,str(Path(__file__).parent))
from full_schema import cluster,load

def test_full_privileged_upgrade_to_execution_and_review_registry():
    with cluster() as pg:
        url=load(*pg)
        from test_tagger_learn_live import _psql
        _psql(url,"CREATE ROLE replicator NOLOGIN",
            "ALTER DEFAULT PRIVILEGES FOR ROLE kura IN SCHEMA public GRANT SELECT ON TABLES TO replicator",
            "ALTER DEFAULT PRIVILEGES FOR ROLE kura_reconciler IN SCHEMA public GRANT SELECT ON TABLES TO replicator")
        env={k:v for k,v in os.environ.items()
             if not k.startswith(("KURA_","PG","DATABASE_"))}
        env.update(DATABASE_URL=url,KURA_MIGRATE_SESSION_AUTHORIZATION="kura")
        result=subprocess.run([sys.executable,"-m","alembic","upgrade","0203"],
            cwd=Path(__file__).parents[1],env=env,capture_output=True,text=True,timeout=90)
        assert result.returncode==0,result.stderr[-6000:]
        assert _psql(url,"SELECT version_num FROM kura_alembic_version")=="0203"
        assert _psql(url,"SELECT count(*) FROM tagger_review_registry_activation")=="0"
        assert _psql(url,"SELECT count(*) FROM tagger_learning_execution")=="0"
        for table in ("tagger_learning_execution","tagger_learning_execution_event",
                      "tagger_review_set","tagger_review_set_member","tagger_review_registry_activation"):
            assert _psql(url,"SELECT has_table_privilege('replicator','"+table+"','SELECT')")=="f"
