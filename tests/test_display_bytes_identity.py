"""Real private PG: moved images keep their display-byte identity."""
import hashlib
from sqlalchemy import text
from test_collector_next import postgres, run  # noqa:F401
from test_pool_search_candidates import search_database  # noqa:F401
from tagger.display_bytes import identities


def test_identity_prefers_legacy_then_view_not_original_or_thumbnail(search_database):
    import psycopg2
    c=psycopg2.connect(search_database)
    try:
        with c.cursor() as q:
            q.execute("CREATE TABLE post(id int primary key,type text,status text,data bytea,sha256 bytea)")
            q.execute("CREATE TABLE post_rendition(post_id int,kind text,sha256 bytea,PRIMARY KEY(post_id,kind))")
            q.execute("INSERT INTO post VALUES(1,'image','ok',%s,%s),(2,'image','ok',NULL,%s),(3,'image','ok',NULL,NULL)",(b'legacy',b'original',b'original'))
            q.execute("INSERT INTO post_rendition VALUES(1,'view',%s),(2,'view',%s),(2,'thumb',%s)",(b'other',hashlib.sha256(b'moved').digest(),b'thumb'))
            def query(sql,args):
                q.execute(sql,args);return q.fetchall()
            rows={row[0]:row for row in identities(query,[1,2,3,2])}
            assert rows[1][3]==hashlib.sha256(b'legacy').hexdigest()
            assert rows[2][3]==hashlib.sha256(b'moved').hexdigest()
            assert rows[3][3] is None
    finally:
        c.rollback();c.close()
