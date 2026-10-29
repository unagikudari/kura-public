#!/usr/bin/env python3
"""Verified, resumable offload of PG-viewable static originals to an external CAS.

Inventory is read-only for source files and PostgreSQL. Run preserves the original
bytes externally, replacing legacy local aliases only after copy/readback checks.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import stat
import time
import tempfile
import urllib.request
from urllib.parse import urlparse
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
from PIL import Image

from api.config import DATABASE_URL_SYNC, ORIGINAL_ARCHIVE_ROOT
from api.services import files, renditions
from api.services.media_materialize import motion_signature

NAME = re.compile(r'^(\d+)_([0-9a-f]{16})\.(jpg|jpeg|png|webp)$')
MAX_SIZE = 32*1024**2


def fsync_dir(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


class MountGuard:
    def __init__(self,mount,root,source,expected=None):
        # source=None: the archive host writing for a node whose source is elsewhere
        # (run-remote, kura-on-nodeb plan rev 8); the source checks then run on that node.
        self.mount=Path(mount).absolute();self.root=Path(root).absolute()
        self.source=Path(source).absolute() if source is not None else None
        if not self.mount.is_mount() or self.root==self.mount or not self.root.resolve().is_relative_to(self.mount.resolve()):
            raise RuntimeError('External archive mount/root is not valid')
        if self.source is not None:
            if self.source.resolve()!=files.storage_directory('content').resolve():
                raise RuntimeError('Source differs from the application content directory')
            if self.source.is_symlink() or not self.source.is_dir():
                raise RuntimeError('Archive/source root must be a real directory')
        if self.root.is_symlink():raise RuntimeError('Archive/source root must be a real directory')
        self.dev=self.mount.stat().st_dev
        self.source_dev=self.source.stat().st_dev if self.source is not None else None
        if self.dev==self.source_dev:raise RuntimeError('Archive must be on a separate filesystem')
        self.root.mkdir(parents=True,exist_ok=True)
        marker=self.root/'.archive-id'
        try:
            with marker.open('x') as out:
                out.write(str(uuid4()));out.flush();os.fsync(out.fileno())
            fsync_dir(self.root)
        except FileExistsError:pass
        self.identity=marker.read_text()
        if expected and (expected['device']!=self.dev or expected['archive_id']!=self.identity or expected['source_device']!=self.source_dev):
            raise RuntimeError('Archive/source identity changed')
        self.check()

    def check(self):
        if (not self.mount.is_mount() or self.mount.stat().st_dev!=self.dev
                or self.root.stat().st_dev!=self.dev
                or (self.source is not None and self.source.stat().st_dev!=self.source_dev)
                or (self.root/'.archive-id').read_text()!=self.identity):
            raise RuntimeError('Archive mount identity lost')

    def header(self):
        return dict(mount=str(self.mount),root=str(self.root),source=str(self.source),device=self.dev,
                    source_device=self.source_dev,archive_id=self.identity,format=1)


@contextmanager
def archive_lock(guard):
    guard.check()
    with (guard.root/'.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if guard.source is None:      # run-remote: the aliases are switched on the source node
            yield
            return
        # Do not race an fs-name migration while changing its legacy aliases.
        with (guard.source.parent/'.fs_salt_migrate.lock').open('a') as migration:
            fcntl.flock(migration,fcntl.LOCK_SH|fcntl.LOCK_NB)
            yield


DECLARATION=Path(__file__).resolve().parents[1]/'tagger'/'deploy'/'nodes.json'   # per-node declarations; KURA_NODES_DECLARATION overrides, as in tagger/kura_db.py


def _header_digest(path):
    plan=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
    try:return hashlib.sha256(plan.execute('SELECT header FROM meta').fetchone()[0].encode()).hexdigest()
    finally:plan.close()


def declared_plan(node=None):
    """This node's plan as tagger/deploy/nodes.json declares it (nodes.<node>.original_archive:
    the per-node declarations every deploy reads, reviewed in git): its path and the sha256 of
    its header, so a plan re-made at the same path is not mistaken for the declared one."""
    node=node or socket.gethostname().split('.')[0]
    path=Path((os.environ.get('KURA_NODES_DECLARATION') or '').strip() or DECLARATION)
    entry=(json.loads(path.read_text()).get('nodes',{}).get(node) or {}).get('original_archive')
    if not entry:raise RuntimeError(f'no original archive plan is declared for {node} (nodes.{node}.original_archive in tagger/deploy/nodes.json)')
    return Path(entry['plan']),entry['header_sha256']


def current_plan(path=None):
    """The plan a writer may use: the declared one. Given a path, it must be that plan; given
    none, the declared one is used. An older plan carries the exclusions and expected hashes of
    its time (2026-10-25: a plan taken before the post repairs refused what they had fixed), so
    writers never use one by accident; restore and status read any plan."""
    declared,digest=declared_plan()
    if path is not None and Path(path).resolve()!=declared.resolve():
        raise RuntimeError(f'{path} is not the declared plan ({declared})')
    if _header_digest(declared)!=digest:
        raise RuntimeError(f'{declared} is not the declared plan: its header differs (re-made?)')
    return declared


def any_plan(path=None):
    """For readers (restore, status): a given plan, else the declared one."""
    return Path(path) if path is not None else declared_plan()[0]


def declared(node=None):
    """What tagger/deploy/nodes.json declares for this node, and whether the plan on
    disk is that plan."""
    plan,digest=declared_plan(node)
    actual=_header_digest(plan) if plan.exists() else None
    return dict(node=node or socket.gethostname().split('.')[0],plan=str(plan),declared_header_sha256=digest,
                on_disk_header_sha256=actual,matches=actual==digest)


def connect_plan(path):
    plan=sqlite3.connect(path);plan.row_factory=sqlite3.Row
    plan.execute('PRAGMA journal_mode=WAL')
    plan.execute('PRAGMA synchronous=FULL');plan.execute('PRAGMA cache_size=-65536')
    return plan


def _inventory_local(path,guard):
    guard.check()
    if ORIGINAL_ARCHIVE_ROOT is None or ORIGINAL_ARCHIVE_ROOT.resolve()!=guard.root.resolve():
        raise RuntimeError('KURA_ORIGINAL_ARCHIVE_ROOT must match this archive')
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('xb'):pass
    plan=connect_plan(path)
    plan.execute('PRAGMA cache_size=-262144');plan.execute('PRAGMA temp_store=MEMORY')
    plan.executescript('''CREATE TABLE meta (header text);
      CREATE TABLE objects (dev int,ino int,pid int,ext text,size int,mtime int,nlink int,allocated int,
        status text DEFAULT 'unclassified',reason text,sha text,md5 text,archive_sha text,
        PRIMARY KEY(dev,ino));
      CREATE TABLE aliases (name text PRIMARY KEY,dev int,ino int);
      CREATE INDEX aliases_inode ON aliases(dev,ino);
      CREATE INDEX pending_size ON objects(status,size DESC);''')
    header=guard.header();header['created_at']=datetime.now(timezone.utc).isoformat()
    plan.execute('INSERT INTO meta VALUES (?)',(json.dumps(header),));plan.commit()
    with plan:
        with os.scandir(guard.source) as entries:
            for entry in entries:
                match=NAME.fullmatch(entry.name)
                if not match or entry.is_symlink() or not entry.is_file(follow_symlinks=False):continue
                st=entry.stat(follow_symlinks=False)
                plan.execute('INSERT OR IGNORE INTO objects(dev,ino,pid,ext,size,mtime,nlink,allocated) VALUES(?,?,?,?,?,?,?,?)',
                  (st.st_dev,st.st_ino,int(match[1]),match[3],st.st_size,st.st_mtime_ns,st.st_nlink,st.st_blocks*512))
                plan.execute('INSERT INTO aliases VALUES(?,?,?)',(entry.name,st.st_dev,st.st_ino))
    pg=psycopg2.connect(DATABASE_URL_SYNC);pg.set_session(readonly=True)
    cursor=plan.execute('SELECT * FROM objects')
    while batch:=cursor.fetchmany(1000):
        ids=list({r['pid'] for r in batch})
        with pg.cursor(cursor_factory=RealDictCursor) as db:
            db.execute('''SELECT id,"mime-type" AS mime,sha256,checksum_md5,checksum,file_size FROM post
              WHERE id=ANY(%s) AND status='ok' AND type IN ('image','animation') AND media_location='pg'
                AND (data IS NOT NULL OR EXISTS (SELECT 1 FROM post_rendition r WHERE r.post_id=post.id AND r.kind='view'))
                AND (thumbnail IS NOT NULL OR EXISTS (SELECT 1 FROM post_rendition r WHERE r.post_id=post.id AND r.kind='thumb'))''',(ids,))
            meta={r['id']:r for r in db}
        pg.rollback()
        with plan:
            for row in batch:
                p=meta.get(row['pid']);reason='not_pg_viewable_original'
                aliases=[r[0] for r in plan.execute('SELECT name FROM aliases WHERE dev=? AND ino=?',(row['dev'],row['ino']))]
                if p:
                    # Compare {post_id}_{hash} only. post."mime-type" names the display WebP
                    # for pg rows (rewritten when it was materialized), so a .jpg/.png original
                    # never matched a .webp expected name: 283,760 originals / 343.6 GB were
                    # excluded as not_original_names in the 2026-10-19 plan for that alone.
                    # The name's hash part is what derives from the salt; the extension is
                    # the file's own (NAME restricts it to the image set).
                    expected={path.stem for path in files.names('content',row['pid'],p['mime'])}
                    md5=p['checksum_md5'] or (p['checksum'] if re.fullmatch('[0-9a-f]{32}',p['checksum'] or '') else None)
                    sha=bytes(p['sha256']).hex() if p['sha256'] else None
                    if not {Path(a).stem for a in aliases}.issubset(expected):reason='not_original_names'
                    elif len(aliases)!=row['nlink']:reason='unobserved_hardlinks'
                    elif row['size']<=0 or row['size']>MAX_SIZE:reason='size_outside_bounded_policy'
                    elif p['file_size']!=row['size']:reason='database_size_mismatch'
                    elif not sha and not re.fullmatch('[0-9a-f]{32}',md5 or ''):reason='no_original_identity'
                    else:reason=None
                    plan.execute('UPDATE objects SET sha=?,md5=? WHERE dev=? AND ino=?',(sha,md5,row['dev'],row['ino']))
                plan.execute('UPDATE objects SET status=?,reason=? WHERE dev=? AND ino=?',('pending' if reason is None else 'excluded',reason,row['dev'],row['ino']))
    pg.close();out=summary(plan);plan.execute('PRAGMA wal_checkpoint(TRUNCATE)');plan.close();fsync_dir(path.parent)
    return out


def inventory(path,guard):
    """Build random-write metadata locally, publish one verified immutable plan."""
    path=Path(path)
    if not path.parent.resolve().is_relative_to(guard.root.resolve()):raise RuntimeError('Plan must be on the archive filesystem')
    if path.exists():raise FileExistsError(path)
    with tempfile.TemporaryDirectory(prefix='kura-archive-plan-') as scratch:
        working=Path(scratch)/'plan.sqlite'
        out=_inventory_local(working,guard)
        guard.check();path.parent.mkdir(parents=True,exist_ok=True)
        temp=path.parent/('.plan-'+uuid4().hex+'.part')
        try:
            with working.open('rb') as inp,temp.open('xb') as dst:
                shutil.copyfileobj(inp,dst,1024**2);dst.flush();os.fsync(dst.fileno())
            with working.open('rb') as inp:expected=hashlib.file_digest(inp,'sha256').hexdigest()
            with temp.open('rb') as inp:
                if hashlib.file_digest(inp,'sha256').hexdigest()!=expected:raise RuntimeError('Plan copy hash mismatch')
            guard.check();os.link(temp,path);fsync_dir(path.parent)
        finally:temp.unlink(missing_ok=True)
    return out


def signature(st):return st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns


def check_aliases(guard,row,names,target=None):
    regular=[]
    for name in names:
        if not NAME.fullmatch(name) or Path(name).name!=name:raise RuntimeError('Invalid source alias')
        path=guard.source/name
        if path.is_symlink():
            if target is None or path.resolve()!=target.resolve():raise RuntimeError('Source alias changed')
        else:
            st=path.lstat()
            if not stat.S_ISREG(st.st_mode) or signature(st)!=(row['dev'],row['ino'],row['size'],row['mtime']):
                raise RuntimeError('Source inode changed')
            regular.append(path)
    if regular and regular[0].stat().st_nlink!=len(regular):raise RuntimeError('Unobserved source hardlinks')
    return regular


def verify_object(target,sha,size,expected_sha=None,expected_md5=None):
    if target.is_symlink() or not target.is_file() or target.stat().st_size!=size:raise RuntimeError('Archive object is not a regular matching file')
    digest=hashlib.sha256();md5=hashlib.md5()
    with target.open('rb') as inp:
        while block:=inp.read(1024**2):digest.update(block);md5.update(block)
    if digest.hexdigest()!=sha or (expected_sha and sha!=expected_sha):raise RuntimeError('Archive object hash mismatch')
    if expected_md5 and md5.hexdigest()!=expected_md5:raise RuntimeError('Archived original MD5 mismatch')


def staging_file(guard,row):
    guard.check()
    if shutil.disk_usage(guard.root).free<row['size']+10*1024**3:raise RuntimeError('Archive reserve would be exhausted')
    staging=guard.root/'staging';staging.mkdir(exist_ok=True)
    return staging/(uuid4().hex+'.part')


def verified_copy(source,guard,row,post=None,any_content=False):
    temp=staging_file(guard,row)
    try:
        sha=hashlib.sha256();md5=hashlib.md5()
        fd=os.open(source,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as inp,temp.open('xb') as out:
            if signature(os.fstat(inp.fileno()))!=(row['dev'],row['ino'],row['size'],row['mtime']):raise RuntimeError('Source changed before copy')
            # An animated original leaves only behind a view that keeps its motion.
            # (A file of no post has no view to keep: any_content copies it as it is.)
            if not any_content:
                if post is None:
                    with Image.open(inp) as image:
                        if getattr(image,'is_animated',False):raise RuntimeError('Animated original needs a motion-preserving policy')
                else:check_motion(inp,post,post['view'])
            inp.seek(0)
            while block:=inp.read(1024**2):out.write(block);sha.update(block);md5.update(block)
            out.flush();os.fsync(out.fileno())
            if signature(os.fstat(inp.fileno()))!=(row['dev'],row['ino'],row['size'],row['mtime']):raise RuntimeError('Source changed during copy')
        return place_staged(temp,guard,row,sha.hexdigest(),md5.hexdigest())
    finally:temp.unlink(missing_ok=True)


def place_staged(temp,guard,row,digest,md5_hex):
    """The archive half of verified_copy: a staged, fsynced local file whose hashes the caller
    computed while writing it becomes the CAS object (kura-on-nodeb plan, rev 8: the source
    half may run on another host and arrive over ssh)."""
    try:
        if row['sha'] and digest!=row['sha']:raise RuntimeError('Original SHA256 mismatch')
        if row['md5'] and md5_hex!=row['md5']:raise RuntimeError('Original MD5 mismatch')
        target=guard.root/'objects'/'sha256'/digest[:2]/digest
        target.parent.mkdir(parents=True,exist_ok=True)
        # The writer briefly opens the prefix under the archive-wide lock.
        # Read-only prefixes also make older write-through code fail safely.
        os.chmod(target.parent,0o755)
        try:
            try:os.link(temp,target)
            except FileExistsError:pass
            verify_object(target,digest,row['size']);os.chmod(target,0o444)
        finally:
            os.chmod(target.parent,0o555)
        for parent in (target.parent,target.parent.parent,target.parent.parent.parent,guard.root):fsync_dir(parent)
        guard.check()
        return target,digest
    finally:temp.unlink(missing_ok=True)


def _display_blobs(db, post):
    """The display bytes the API serves: the rendition first, the legacy column only when
    there is none (api/routers/posts.py has the same order)."""
    blobs=[]
    for kind,column in (("view","data"),("thumb","thumbnail")):
        db.execute(renditions.psycopg(renditions.LOOKUP), {"post_id":post['id'],"kind":kind})
        obj=db.fetchone()
        if obj is not None:
            blob=renditions.path_for(bytes(obj['sha256']),obj['mime']).read_bytes()
            if hashlib.sha256(blob).digest()!=bytes(obj['sha256']):
                raise RuntimeError('Rendition file differs from its address')
        elif post[column] is not None:
            blob=post[column]
        else:raise RuntimeError('Static WebP representation is not available')
        blobs.append(blob)
    return blobs


def viewable(data,thumbnail,ptype='image'):
    """The display renditions an archived original leaves behind: a static WebP thumbnail,
    and a view that is a static WebP for an image or an animated WebP for an animation
    (whose motion is checked against the original where the original is read)."""
    for body,animated in ((data,ptype=='animation'),(thumbnail,False)):
        with Image.open(io.BytesIO(bytes(body))) as image:
            if image.format!='WEBP' or bool(getattr(image,'is_animated',False))!=animated:
                raise RuntimeError('Static WebP representation is not available' if not animated
                                   else 'Animated WebP view is not available')
            image.load()


def recheck_motion(post,path):
    """The motion check again at the switch, under the locks, against the view as it is now:
    a view replaced after the offer or the copy must not let the original go."""
    check_motion(path,post,post['view'])


# Whether a view keeps an original's motion, by (sha256 of the original, sha256 of the view):
# the answer depends only on those two contents, and decoding every frame of both is what an
# archive batch spends its time on (2026-10-25: 42 s of 60 in WebP frame decoding, each
# animation decoded four times). The post-type rules are applied on every call.
_MOTION={}


def _read(source):
    if isinstance(source,(str,os.PathLike)):return Path(source).read_bytes()
    pos=source.tell();source.seek(0)
    try:return source.read()
    finally:source.seek(pos)


def motion_entry(source,view):
    """((original sha256, view sha256), keeps its motion) for an animated original, else None.
    The digest and the decode come from the one read of each content, so a file replaced
    meanwhile cannot put one content's answer under another's key."""
    data,shown_bytes=_read(source),bytes(view)
    key=(hashlib.sha256(data).hexdigest(),hashlib.sha256(shown_bytes).hexdigest())
    with Image.open(io.BytesIO(data)) as original:
        if not getattr(original,'is_animated',False):return None
        with Image.open(io.BytesIO(shown_bytes)) as shown:
            return key,motion_signature(original)==motion_signature(shown)


def check_motion(source,post,view):
    """An animated original may leave the node only when the post is an animation whose view
    keeps its motion: same frames (identical consecutive frames merged), durations, loop and
    use of alpha (api/services/media_materialize.motion_signature). Anything else stays local.
    `source` is the original's path or open binary file."""
    data=_read(source)
    with Image.open(io.BytesIO(data)) as original:animated=bool(getattr(original,'is_animated',False))
    if not animated:
        if post['type']=='animation':raise RuntimeError('Animation post with a still original')
        return
    if post['type']!='animation':raise RuntimeError('Animated original needs a motion-preserving policy')
    key=(hashlib.sha256(data).hexdigest(),hashlib.sha256(bytes(view)).hexdigest())
    if key not in _MOTION:
        found=motion_entry(io.BytesIO(data),view)
        _MOTION[found[0]]=found[1]
    if not _MOTION[key]:raise RuntimeError('Animated view does not keep the original motion')


def prefetch_motion(guard,db,rows,workers):
    """Start the motion checks of a batch's animations in worker processes: the serial loop
    then finds them in _MOTION (keyed by content, so a file or view changed since is simply
    checked again). Returns {(dev, ino): future}."""
    names={};futures={}
    for row in rows:names.setdefault(row['pid'],[]).append(row)
    if not names or workers<1:return {},None
    db.execute("SELECT id,type,data,thumbnail FROM post WHERE id=ANY(%s) AND type='animation'",(list(names),))
    posts=db.fetchall()
    if not posts:return {},None
    from concurrent.futures import ProcessPoolExecutor
    pool=ProcessPoolExecutor(max_workers=workers)
    for post in posts:
        try:view=_display_blobs(db,post)[0]
        except Exception:continue
        for row in names[post['id']]:
            alias=next(iter(r[0] for r in row['aliases']),None)
            if alias:futures[(row['dev'],row['ino'])]=pool.submit(motion_entry,str(guard.source/alias),bytes(view))
    return futures,pool


def switch_aliases(guard,row,names,target):
    guard.check();check_aliases(guard,row,names,target)
    verify_object(target,row['archive_sha'],row['size'],row['sha'],row['md5'])
    relink(guard,names,target)


def relink(guard,names,target):
    """Replace each regular alias with a symlink to target, then confirm every name points
    there. The caller has checked the aliases and verified the object (locally, or on
    another host by reading it back: kura-on-nodeb plan rev 8). The link text is compared,
    not resolved, so a target on another machine is fine."""
    for name in names:
        path=guard.source/name
        if path.is_symlink():continue
        guard.check()
        temp=path.with_name(path.name+'.archive-'+uuid4().hex)
        try:
            os.symlink(str(target),temp);os.replace(temp,path)
        finally:temp.unlink(missing_ok=True)
    fsync_dir(guard.source)
    if any(not (guard.source/name).is_symlink() or os.readlink(guard.source/name)!=str(target) for name in names):
        raise RuntimeError('Archive alias switch did not complete')


def locked_eligible_post(db,row,names):
    """Lock the post row (FOR SHARE, 2 s lock timeout) and re-check what makes its original
    eligible, just before anything is switched: same post, size and identity as inventoried,
    the names are the current filename policy's, and the static display renditions are valid.
    Same lock order as API writers/deleters: DB row first, then the file stripe."""
    db.execute("SET LOCAL lock_timeout='2s'")
    db.execute('''SELECT id,type,sha256,checksum_md5,checksum,"mime-type" AS mime,file_size,data,thumbnail FROM post
        WHERE id=%s AND status='ok' AND type IN ('image','animation') AND media_location='pg' FOR SHARE''',(row['pid'],))
    post=db.fetchone()
    if not post or post['file_size']!=row['size']:raise RuntimeError('Post metadata changed')
    current_sha=bytes(post['sha256']).hex() if post['sha256'] else None
    current_md5=post['checksum_md5'] or (post['checksum'] if re.fullmatch('[0-9a-f]{32}',post['checksum'] or '') else None)
    if current_sha!=row['sha'] or current_md5!=row['md5']:raise RuntimeError('Post identity changed')
    if not {Path(n).stem for n in names}.issubset({p.stem for p in files.names('content',row['pid'],post['mime'])}):raise RuntimeError('Current filename policy differs')
    post['view'],thumb=_display_blobs(db,post)
    viewable(post['view'],thumb,post['type'])
    return post


def summary(plan):
    return {r[0]:dict(files=r[1],bytes=r[2],allocated_bytes=r[3]) for r in plan.execute('SELECT status,count(*),sum(size),sum(allocated) FROM objects GROUP BY status')}


def runtime_attestation():
    from scripts.media_replication_gate import _serving_worker_pids
    from scripts.kura_http_key import install_api_key_opener
    install_api_key_opener()
    base=(os.environ.get('KURA_LOCAL_URL') or os.environ.get('KURA_URL') or '').rstrip('/')
    parsed=urlparse(base)
    if parsed.scheme not in ('http','https') or parsed.hostname not in ('127.0.0.1','::1','localhost') or parsed.path:
        raise RuntimeError('Archive requires an explicit node-local API base URL')
    port=parsed.port or (443 if parsed.scheme=='https' else 80)
    expected=_serving_worker_pids('kura-api.service',port)
    if not expected:raise RuntimeError('No serving API workers observed')
    observed=set();deadline=time.monotonic()+15
    while observed!=expected and time.monotonic()<deadline:
        with urllib.request.urlopen(base+'/api/media-authority',timeout=5) as response:
            probe=json.load(response)
        if probe.get('original_archive_policy')!=1 or probe.get('role')!='authority':
            raise RuntimeError('A serving API worker lacks the immutable archive policy')
        observed.add(probe['pid'])
    if observed!=expected or _serving_worker_pids('kura-api.service',port)!=expected:
        raise RuntimeError('Serving worker coverage is incomplete or changed')
    def check():
        if _serving_worker_pids('kura-api.service',port)!=expected:
            raise RuntimeError('Serving API worker set changed during archive batch')
    return check


def run_plan(path,limit=500,seconds=120,workers=8):
    path=current_plan(path)
    plan=connect_plan(path);header=json.loads(plan.execute('SELECT header FROM meta').fetchone()[0])
    guard=MountGuard(header['mount'],header['root'],header['source'],header)
    if ORIGINAL_ARCHIVE_ROOT is None or ORIGINAL_ARCHIVE_ROOT.resolve()!=guard.root.resolve():raise RuntimeError('Application archive policy differs')
    check_runtime=runtime_attestation()
    deadline=time.monotonic()+seconds
    with archive_lock(guard),closing(psycopg2.connect(DATABASE_URL_SYNC)) as pg:
        rows=plan.execute("SELECT * FROM objects WHERE status IN ('pending','verified') ORDER BY size DESC LIMIT ?",(limit,)).fetchall()
        with pg,pg.cursor(cursor_factory=RealDictCursor) as db:
            futures,pool=prefetch_motion(guard,db,[dict(r,aliases=plan.execute('SELECT name FROM aliases WHERE dev=? AND ino=?',(r['dev'],r['ino'])).fetchall()) for r in rows],workers)
        try:
            _run_rows(plan,pg,guard,rows,futures,deadline,check_runtime)
        finally:
            if pool:pool.shutdown(wait=False,cancel_futures=True)
    return summary(plan)


def _run_rows(plan,pg,guard,rows,futures,deadline,check_runtime):
    for row in rows:
        if time.monotonic()>deadline:break
        future=futures.get((row['dev'],row['ino']))
        if future is not None:
            try:
                found=future.result(timeout=max(0,deadline-time.monotonic()))
                if found:_MOTION[found[0]]=found[1]
            except Exception:pass          # checked inline below
            if time.monotonic()>deadline:break
        guard.check();check_runtime();names=[r[0] for r in plan.execute('SELECT name FROM aliases WHERE dev=? AND ino=?',(row['dev'],row['ino']))]
        try:
            with pg:
                with pg.cursor(cursor_factory=RealDictCursor) as db:
                    post=locked_eligible_post(db,row,names)
                    with files.content_lock(row['pid']):
                        if row['archive_sha'] and not re.fullmatch('[0-9a-f]{64}',row['archive_sha']):raise RuntimeError('Invalid archive digest')
                        target=guard.root/'objects'/'sha256'/row['archive_sha'][:2]/row['archive_sha'] if row['archive_sha'] else None
                        regular=check_aliases(guard,row,names,target)
                        if row['status']=='pending':
                            if not regular:raise RuntimeError('No pinned local original')
                            target,digest=verified_copy(regular[0],guard,row,post)
                            with plan:plan.execute("UPDATE objects SET status='verified',archive_sha=? WHERE dev=? AND ino=?",(digest,row['dev'],row['ino']))
                            row=dict(row);row['archive_sha']=digest
                        recheck_motion(post,target)
                        switch_aliases(guard,row,names,target)
            with plan:plan.execute("UPDATE objects SET status='done',reason=NULL WHERE dev=? AND ino=?",(row['dev'],row['ino']))
        except Exception as exc:
            # A mount failure halts the batch. Other changes remain inspectable.
            guard.check()
            with plan:plan.execute("UPDATE objects SET status=?,reason=? WHERE dev=? AND ino=?",('verified' if row['archive_sha'] else 'refused',str(exc)[:180],row['dev'],row['ino']))


def restore(path,post_id):
    path=any_plan(path)
    plan=connect_plan(path);header=json.loads(plan.execute('SELECT header FROM meta').fetchone()[0])
    guard=MountGuard(header['mount'],header['root'],header['source'],header)
    runtime_attestation()()
    rows=plan.execute("SELECT * FROM objects WHERE pid=? AND status='done'",(post_id,)).fetchall()
    if len(rows)!=1:raise RuntimeError('Exactly one completed original is required')
    row=rows[0];sha=row['archive_sha']
    if not re.fullmatch('[0-9a-f]{64}',sha or ''):raise RuntimeError('Invalid archive digest')
    target=guard.root/'objects'/'sha256'/sha[:2]/sha
    with archive_lock(guard),psycopg2.connect(DATABASE_URL_SYNC) as pg:
        with pg.cursor(cursor_factory=RealDictCursor) as db:
            db.execute('SELECT sha256,checksum_md5,checksum,"mime-type" AS mime FROM post WHERE id=%s FOR SHARE',(post_id,))
            post=db.fetchone()
            if not post:raise RuntimeError('Post no longer exists')
            current_sha=bytes(post['sha256']).hex() if post['sha256'] else None
            current_md5=post['checksum_md5'] or (post['checksum'] if re.fullmatch('[0-9a-f]{32}',post['checksum'] or '') else None)
            if current_sha!=row['sha'] or current_md5!=row['md5']:raise RuntimeError('Post identity changed')
            aliases=[r[0] for r in plan.execute('SELECT name FROM aliases WHERE dev=? AND ino=?',(row['dev'],row['ino']))]
            expected={p.stem for p in files.names('content',post_id,post['mime'])}
            if not aliases or any(not NAME.fullmatch(n) or Path(n).stem not in expected for n in aliases):
                raise RuntimeError('Current filename policy differs')
            # Restore the exact original aliases recorded by the plan, not the
            # extension of the display WebP stored in post.mime-type.
            names=[guard.source/n for n in aliases]
            if any(not p.is_symlink() or p.resolve()!=target.resolve() for p in names):raise RuntimeError('Active original names changed')
            verify_object(target,sha,row['size'],row['sha'],row['md5']);guard.check()
            files.write_all_names(names,target.read_bytes())
            if any(p.is_symlink() or p.stat().st_size!=row['size'] for p in names):raise RuntimeError('Local restore incomplete')
            fsync_dir(names[0].parent)
    with plan:plan.execute("UPDATE objects SET status='restored' WHERE dev=? AND ino=?",(row['dev'],row['ino']))
    return summary(plan)


# ── the archive on another host (docs/notes/kura-on-nodeb-plan.md, "Originals on another
# host", rev 8) ──────────────────────────────────────────────────────────────────────────
#
# The node with the originals (nodeb) and the node with the archive disk (primary) split the job:
#
#   nodeb  inventory-remote   plan of local originals (as `inventory`, without the mount)
#   primary   run-remote          asks nodeb for offers, streams each one into its own staging
#                             file, places it with place_staged (MountGuard, archive_lock,
#                             read-back, fsync, exactly as a local run), then asks nodeb to
#                             commit
#   nodeb  remote-offer        eligible originals → `offers` rows (identity fixed here)
#   nodeb  remote-read         that offer's file only, O_NOFOLLOW, signature checked before
#                             and after; non-zero exit if it changed → primary discards the bytes
#   nodeb  remote-commit       read the object back from the archive node (outside every
#                             lock), then under the post row lock and content lock: the same
#                             re-checks as a local run, check_aliases, relink; idempotent
#
# primary reaches nodeb's three commands through one ssh key whose authorized_keys entry forces
# `remote-dispatch`, which accepts exactly those commands and their arguments.

OFFER_ID=re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
SHA_HEX=re.compile(r'^[0-9a-f]{64}$')


class SourceGuard:
    """The source side on a node whose archive disk is on another host."""

    def __init__(self,source,archive_root,archive_id,expected=None):
        self.source=Path(source).absolute();self.root=Path(archive_root);self.mount=None;self.dev=None
        if self.source.resolve()!=files.storage_directory('content').resolve():
            raise RuntimeError('Source differs from the application content directory')
        if self.source.is_symlink() or not self.source.is_dir():raise RuntimeError('Source must be a real directory')
        if not OFFER_ID.fullmatch(archive_id or ''):raise RuntimeError('KURA_ORIGINAL_ARCHIVE_ID must be the archive uuid')
        self.identity=archive_id;self.source_dev=self.source.stat().st_dev
        if expected and (expected.get('format')!=2 or expected['archive_id']!=self.identity
                         or expected['source_device']!=self.source_dev or expected['root']!=str(self.root)):
            raise RuntimeError('Archive/source identity changed')

    def check(self):
        if self.source.stat().st_dev!=self.source_dev:raise RuntimeError('Source identity lost')

    def header(self):
        return dict(mount=None,root=str(self.root),source=str(self.source),device=None,
                    source_device=self.source_dev,archive_id=self.identity,format=2)


@contextmanager
def source_lock(plan_path,guard):
    with (Path(plan_path).parent/'.archive-remote.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with (guard.source.parent/'.fs_salt_migrate.lock').open('a') as migration:
            fcntl.flock(migration,fcntl.LOCK_SH|fcntl.LOCK_NB)
            yield


def _offers_table(plan):
    plan.execute('''CREATE TABLE IF NOT EXISTS offers (offer_id text PRIMARY KEY,dev int,ino int,pid int,
        names text,size int,mtime int,sha text,md5 text,state text,reason text,archive_sha text,
        created_at text,updated_at text)''')
    # The post's type, so the archive node knows an animated original was offered with a
    # motion-preserving view (checked by the source, where the view is).
    if 'ptype' not in {r[1] for r in plan.execute('PRAGMA table_info(offers)')}:
        plan.execute('ALTER TABLE offers ADD COLUMN ptype text')
    plan.commit()


def _now():return datetime.now(timezone.utc).isoformat()


def inventory_remote(path,source,archive_root,archive_id):
    if ORIGINAL_ARCHIVE_ROOT is None or ORIGINAL_ARCHIVE_ROOT!=Path(archive_root):
        raise RuntimeError('KURA_ORIGINAL_ARCHIVE_ROOT must name the archive root on the archive node')
    guard=SourceGuard(source,archive_root,archive_id)
    path=Path(path)
    if path.exists():raise FileExistsError(path)
    out=_inventory_local(path,guard)
    plan=connect_plan(path);_offers_table(plan);plan.close()
    return out


def _remote_guard(plan):
    header=json.loads(plan.execute('SELECT header FROM meta').fetchone()[0])
    from api.config import ARCHIVE_ID
    return SourceGuard(header['source'],header['root'],ARCHIVE_ID,header)


def remote_offer(plan_path,limit=50):
    """Offers: open ones first (primary may have restarted), then new eligible originals."""
    plan_path=current_plan(plan_path)
    plan=connect_plan(plan_path);_offers_table(plan);guard=_remote_guard(plan)
    out=[]
    with source_lock(plan_path,guard),closing(psycopg2.connect(DATABASE_URL_SYNC)) as pg:
        for o in plan.execute("SELECT * FROM offers WHERE state IN ('offered','read','committing') ORDER BY created_at LIMIT ?",(limit,)).fetchall():
            # 'committing' carries its digest: the archive node goes straight to the commit
            # (the aliases may already be links, so there is nothing left to read).
            out.append(dict(offer_id=o['offer_id'],pid=o['pid'],size=o['size'],sha=o['sha'],md5=o['md5'],
                            commit_sha=o['archive_sha'] if o['state']=='committing' else None,ptype=o['ptype']))
        rows=plan.execute('''SELECT * FROM objects o WHERE status='pending' AND NOT EXISTS
            (SELECT 1 FROM offers f WHERE f.dev=o.dev AND f.ino=o.ino AND f.state IN ('offered','read','committing'))
            ORDER BY size DESC LIMIT ?''',(max(0,limit-len(out)),)).fetchall()
        for row in rows:
            guard.check();names=[r[0] for r in plan.execute('SELECT name FROM aliases WHERE dev=? AND ino=?',(row['dev'],row['ino']))]
            try:
                with pg:
                    with pg.cursor(cursor_factory=RealDictCursor) as db:
                        post=locked_eligible_post(db,row,names)
                        with files.content_lock(row['pid']):
                            regular=check_aliases(guard,row,names)
                            if not regular:raise RuntimeError('No pinned local original')
                            # Checked here, where the view is: the archive node only sees the
                            # original (rehearsal 2026-10-25: the 20 largest pending were animated).
                            check_motion(regular[0],post,post['view'])
                offer_id=str(uuid4())
                with plan:plan.execute('INSERT INTO offers (offer_id,dev,ino,pid,names,size,mtime,sha,md5,state,reason,archive_sha,created_at,updated_at,ptype) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?,?)',
                    (offer_id,row['dev'],row['ino'],row['pid'],json.dumps(names),row['size'],row['mtime'],
                     row['sha'],row['md5'],'offered',_now(),_now(),post['type']))
                out.append(dict(offer_id=offer_id,pid=row['pid'],size=row['size'],sha=row['sha'],md5=row['md5'],commit_sha=None,ptype=post['type']))
            except Exception as exc:
                guard.check()
                with plan:plan.execute("UPDATE objects SET status='refused',reason=? WHERE dev=? AND ino=?",(str(exc)[:180],row['dev'],row['ino']))
    return out


def _offer(plan,offer_id):
    if not OFFER_ID.fullmatch(offer_id or ''):raise RuntimeError('Invalid offer id')
    o=plan.execute('SELECT * FROM offers WHERE offer_id=?',(offer_id,)).fetchone()
    if o is None:raise RuntimeError('Unknown offer')
    return dict(o)


def _refuse(plan,offer_id,reason):
    # Only an open offer can be refused: committing/committed never go back (codex 1.270.0).
    with plan:plan.execute("UPDATE offers SET state='refused',reason=?,updated_at=? WHERE offer_id=? AND state IN ('offered','read')",(reason[:180],_now(),offer_id))


def remote_read(plan_path,offer_id,out):
    """Stream exactly the offered file. Exit status is the contract: 0 only if the file was
    the offered one before and after streaming; primary discards the bytes otherwise."""
    plan_path=current_plan(plan_path)
    plan=connect_plan(plan_path);guard=_remote_guard(plan);o=_offer(plan,offer_id)
    if o['state'] not in ('offered','read'):raise RuntimeError(f"Offer is {o['state']}")
    expected=(o['dev'],o['ino'],o['size'],o['mtime'])
    for name in json.loads(o['names']):
        path=guard.source/name
        if path.is_symlink():continue
        try:fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        except OSError:continue
        with os.fdopen(fd,'rb') as inp:
            if signature(os.fstat(inp.fileno()))!=expected:
                _refuse(plan,offer_id,'Source changed before read');raise RuntimeError('Source changed before read')
            while block:=inp.read(1024**2):out.write(block)
            out.flush()
            if signature(os.fstat(inp.fileno()))!=expected:
                _refuse(plan,offer_id,'Source changed during read');raise RuntimeError('Source changed during read')
        with plan:plan.execute("UPDATE offers SET state='read',updated_at=? WHERE offer_id=? AND state='offered'",(_now(),offer_id))
        if _offer(plan,offer_id)['state'] not in ('read',):raise RuntimeError('Offer changed state during the read')
        return
    _refuse(plan,offer_id,'No regular alias left');raise RuntimeError('No regular alias left')


def _read_back(o,digest):
    """The object as the archive node serves it, verified against the offer."""
    from api.services import originals
    loc=originals.Location('cas',Path(''),sha=digest)
    if not (o['sha'] or o['md5']):raise RuntimeError('Offer has no identity to verify against')
    try:data=originals.fetch_fresh(loc,originals.Identity(o['sha'],o['md5'],o['size']))
    except originals.OriginalUnavailable as exc:raise RuntimeError(f'Read-back failed: {exc}') from None
    if hashlib.sha256(data).hexdigest()!=digest:raise RuntimeError('Read-back hash differs from the committed digest')


def _switched(guard,names,target):
    return [n for n in names if (guard.source/n).is_symlink() and os.readlink(guard.source/n)==str(target)]


def remote_commit(plan_path,offer_id,digest,read_back=None,attest=None):
    """read → committing(digest) → committed, or → refused when it fails before any alias
    was switched. Every transition is conditional on the current state AND digest, so two
    commits of one offer can never bind it to different digests (codex review of 1.270.1)."""
    plan_path=current_plan(plan_path)
    plan=connect_plan(plan_path);guard=_remote_guard(plan);o=_offer(plan,offer_id)
    if not SHA_HEX.fullmatch(digest or ''):raise RuntimeError('Invalid digest')
    if o['state'] in ('committed','committing') and o['archive_sha']!=digest:
        raise RuntimeError('Offer is bound to another digest')
    if o['state']=='committed':return 'already committed'
    if o['state'] not in ('read','committing'):raise RuntimeError(f"Offer is {o['state']}")
    if o['sha'] and o['sha']!=digest:_refuse(plan,offer_id,'Digest differs from the offer');raise RuntimeError('Digest differs from the offer')
    # 1. Outside every lock: the running API workers read remotely from this archive, and
    #    the archive node's copy, fetched fresh and verified against the OFFER's identity
    #    (SHA-256, or MD5 when the post has none) and then against the digest.
    check_workers=(attest or remote_attestation)()
    (read_back or _read_back)(o,digest)
    # Bind the digest before any alias changes (a stop mid-switch resumes as a commit of the
    # same digest). From 'read' only; resuming 'committing' must already carry this digest.
    with plan:
        if o['state']=='read':
            cur=plan.execute("UPDATE offers SET state='committing',archive_sha=?,updated_at=? WHERE offer_id=? AND state='read'",(digest,_now(),offer_id))
        else:
            cur=plan.execute("UPDATE offers SET updated_at=? WHERE offer_id=? AND state='committing' AND archive_sha=?",(_now(),offer_id,digest))
        if cur.rowcount!=1:raise RuntimeError('Offer changed state concurrently')
    # 2. Under the post row lock and the content lock: local work only.
    names=json.loads(o['names']);row=dict(o);row['archive_sha']=digest
    target=guard.root/'objects'/'sha256'/digest[:2]/digest
    def close_offer(exc):
        # Called only while holding the source lock and the post row lock (and, for an alias
        # mismatch, the content lock), and only for a mismatch the checks themselves found
        # with no alias switched: the
        # original is still a local file and the post or its names changed (deleted,
        # replaced, renamed). Closing it here cannot race another commit of the offer, which
        # would need the same locks (codex review of 1.270.2). Everything else — a lock not
        # obtained, a database timeout, a worker change — leaves the offer 'committing' to
        # be resumed.
        if _switched(guard,names,target):return
        with plan:
            plan.execute("UPDATE offers SET state='refused',reason=?,updated_at=? WHERE offer_id=? AND state='committing' AND archive_sha=?",
                         (f'before switch: {exc}'[:180],_now(),offer_id,digest))
            plan.execute("UPDATE objects SET status='refused',reason=? WHERE dev=? AND ino=?",(f'before switch: {exc}'[:180],o['dev'],o['ino']))

    with source_lock(plan_path,guard),closing(psycopg2.connect(DATABASE_URL_SYNC)) as pg:
        with pg:
            with pg.cursor(cursor_factory=RealDictCursor) as db:
                try:post=locked_eligible_post(db,row,names)
                except RuntimeError as exc:close_offer(exc);raise
                with files.content_lock(o['pid']):
                    check_workers()
                    try:
                        regular=check_aliases(guard,row,names,target)
                        if regular:recheck_motion(post,regular[0])
                    except RuntimeError as exc:close_offer(exc);raise
                    relink(guard,names,target)
                    check_workers()
    with plan:
        cur=plan.execute("UPDATE offers SET state='committed',updated_at=? WHERE offer_id=? AND state='committing' AND archive_sha=?",(_now(),offer_id,digest))
        if cur.rowcount!=1:raise RuntimeError('Offer changed state concurrently')
        plan.execute("UPDATE objects SET status='done',reason=NULL,archive_sha=? WHERE dev=? AND ino=?",(digest,o['dev'],o['ino']))
    return 'committed'


def remote_attestation():
    """Every serving API worker reads archived originals remotely from the expected archive.
    Same shape as runtime_attestation (pid-exact coverage of the cgroup's workers); the HTTP
    probes run before any lock, and the returned check (local, /proc only) is called inside
    the locks to notice a worker change (codex review of 1.270.0)."""
    from scripts.media_replication_gate import _serving_worker_pids
    from scripts.kura_http_key import install_api_key_opener
    from api.config import ARCHIVE_ID
    install_api_key_opener()
    base=(os.environ.get('KURA_LOCAL_URL') or os.environ.get('KURA_URL') or '').rstrip('/')
    parsed=urlparse(base)
    if parsed.scheme not in ('http','https') or parsed.hostname not in ('127.0.0.1','::1','localhost') or parsed.path:
        raise RuntimeError('Archive requires an explicit node-local API base URL')
    port=parsed.port or (443 if parsed.scheme=='https' else 80)
    expected=_serving_worker_pids('kura-api.service',port)
    if not expected:raise RuntimeError('No serving API workers observed')
    observed=set();deadline=time.monotonic()+15
    while observed!=expected and time.monotonic()<deadline:
        with urllib.request.urlopen(base+'/api/media-authority',timeout=5) as response:
            probe=json.load(response)
        if probe.get('original_reader')!={'remote':True,'archive_id':ARCHIVE_ID}:
            raise RuntimeError('A serving API worker does not read archived originals from this archive')
        observed.add(probe['pid'])
    if observed!=expected or _serving_worker_pids('kura-api.service',port)!=expected:
        raise RuntimeError('Serving worker coverage is incomplete or changed')
    def check():
        if _serving_worker_pids('kura-api.service',port)!=expected:
            raise RuntimeError('Serving API worker set changed during the switch')
    return check


REFUSE_REASONS=('animated','size_mismatch','hash_mismatch','unreadable')


def remote_refuse(plan_path,offer_id,reason):
    """The archive node found the offered bytes permanently ineligible: close the offer and
    its object so they are not offered again. Only an open offer (offered/read) is closed."""
    plan_path=current_plan(plan_path)
    if reason not in REFUSE_REASONS:raise RuntimeError('Unknown refusal reason')
    plan=connect_plan(plan_path);o=_offer(plan,offer_id)
    with plan:
        cur=plan.execute("UPDATE offers SET state='refused',reason=?,updated_at=? WHERE offer_id=? AND state IN ('offered','read')",
                         (f'archive node: {reason}',_now(),offer_id))
        if cur.rowcount==1:
            plan.execute("UPDATE objects SET status='refused',reason=? WHERE dev=? AND ino=?",(f'archive node: {reason}',o['dev'],o['ino']))
    return 'refused' if cur.rowcount==1 else f"unchanged ({o['state']})"


def remote_dispatch(argv_text,plan_path,out=None):
    """The forced command for the archive node's key: exactly three commands."""
    import shlex
    args=shlex.split(argv_text or '')
    if args[:1]==['remote-offer'] and len(args)==3 and args[1]=='--limit' and args[2].isdigit() and 0<int(args[2])<=500:
        for offer in remote_offer(plan_path,int(args[2])):print(json.dumps(offer),flush=True)
        return 0
    if args[:1]==['remote-read'] and len(args)==3 and args[1]=='--offer':
        remote_read(plan_path,args[2],out or sys.stdout.buffer);return 0
    if args[:1]==['remote-commit'] and len(args)==5 and args[1]=='--offer' and args[3]=='--sha':
        print(remote_commit(plan_path,args[2],args[4]),flush=True);return 0
    if args[:1]==['remote-refuse'] and len(args)==5 and args[1]=='--offer' and args[3]=='--reason':
        print(remote_refuse(plan_path,args[2],args[4]),flush=True);return 0
    if args==['remote-status']:
        # What the forced command actually runs with — no secret values.
        from api import config
        from urllib.parse import urlsplit
        db=urlsplit(DATABASE_URL_SYNC)
        plan=connect_plan(plan_path) if plan_path is not None and Path(plan_path).exists() else None
        print(json.dumps(dict(data_dir=str(config.DATA_DIR),db_host=db.hostname,db_port=db.port,
            archive_root=str(ORIGINAL_ARCHIVE_ROOT) if ORIGINAL_ARCHIVE_ROOT else None,
            archive_id=config.ARCHIVE_ID or None,archive_url=config.ARCHIVE_OBJECTS_URL or None,
            plan=str(plan_path) if plan_path else None,plan_summary=summary(plan) if plan else None),sort_keys=True),flush=True)
        return 0
    raise RuntimeError('Command not allowed')


def ssh_transport(host,key):
    """How the archive node reaches the source node: one restricted ssh key."""
    def call(command,stdout=None):
        argv=['ssh','-i',str(key),'-o','BatchMode=yes','-o','ConnectTimeout=10',host,command]
        if stdout is None:
            proc=subprocess.run(argv,capture_output=True,text=True,timeout=300)
            return proc.returncode,proc.stdout
        proc=subprocess.run(argv,stdout=stdout,stderr=subprocess.PIPE,timeout=600)
        return proc.returncode,''
    return call


def run_remote(mount,root,transport,limit=50,seconds=120,expected_archive_id=None):
    """On the archive node: archive the source node's offered originals into the local disk."""
    guard=MountGuard(mount,root,None)
    if expected_archive_id and guard.identity!=expected_archive_id:raise RuntimeError('Archive identity differs from KURA_ORIGINAL_ARCHIVE_ID')
    deadline=time.monotonic()+seconds;done=refused=0
    with archive_lock(guard):
        rc,text=transport(f'remote-offer --limit {int(limit)}')
        if rc!=0:raise RuntimeError(f'remote-offer failed ({rc})')
        for line in text.splitlines():
            if time.monotonic()>deadline:break
            o=json.loads(line);guard.check()
            if not OFFER_ID.fullmatch(o['offer_id']):raise RuntimeError('Invalid offer id from the source')
            if o.get('commit_sha'):
                if not SHA_HEX.fullmatch(o['commit_sha']):raise RuntimeError('Invalid digest from the source')
                obj=guard.root/'objects'/'sha256'/o['commit_sha'][:2]/o['commit_sha']
                try:verify_object(obj,o['commit_sha'],o['size'],o['sha'],o['md5'])
                except Exception:refused+=1;continue
                rc,_=transport(f"remote-commit --offer {o['offer_id']} --sha {o['commit_sha']}")
                if rc==0:done+=1
                else:refused+=1
                continue
            row=dict(sha=o['sha'],md5=o['md5'],size=o['size'])
            temp=staging_file(guard,row)
            try:
                with temp.open('xb') as staged:
                    rc,_=transport(f"remote-read --offer {o['offer_id']}",stdout=staged)
                    staged.flush();os.fsync(staged.fileno())
                if rc!=0:refused+=1;continue          # source changed or unreadable: discard
                if temp.stat().st_size!=o['size']:
                    transport(f"remote-refuse --offer {o['offer_id']} --reason size_mismatch");refused+=1;continue
                sha=hashlib.sha256();md5=hashlib.md5()
                with temp.open('rb') as staged:
                    while block:=staged.read(1024**2):sha.update(block);md5.update(block)
                    staged.seek(0)
                    try:
                        with Image.open(staged) as image:animated=getattr(image,'is_animated',False)
                    except Exception:
                        transport(f"remote-refuse --offer {o['offer_id']} --reason unreadable");refused+=1;continue
                if animated and o.get('ptype')!='animation':
                    # Permanently ineligible: tell the source so the offer stops coming back.
                    transport(f"remote-refuse --offer {o['offer_id']} --reason animated");refused+=1;continue
                if (o['sha'] and sha.hexdigest()!=o['sha']) or (o['md5'] and md5.hexdigest()!=o['md5']):
                    transport(f"remote-refuse --offer {o['offer_id']} --reason hash_mismatch");refused+=1;continue
                target,digest=place_staged(temp,guard,row,sha.hexdigest(),md5.hexdigest())
            except Exception as exc:
                print(f'run-remote: offer {o["offer_id"]}: {type(exc).__name__}: {exc}',file=sys.stderr)
                refused+=1;continue
            finally:temp.unlink(missing_ok=True)
            rc,_=transport(f"remote-commit --offer {o['offer_id']} --sha {digest}")
            if rc==0:done+=1
            else:refused+=1
    return dict(done=done,refused=refused)


ANY_NAME=re.compile(r'^(\d+)_([0-9a-f]{16})\.([0-9a-z]{1,5})$')


def orphans(mount,root,source,remove=False,limit=0,seal=None):
    """Files under the source named for a post id that has no post: copied into the CAS with the
    same staging and read-back as an original, listed in an append-only manifest on the archive
    disk (<root>/orphans/manifest.jsonl: name, sha256, size, mtime), and — with remove, only
    under a proven seal — taken out of the source after re-checking each one. Nothing is removed
    that is not in the CAS.

    Removing a file of no post is safe only while nothing can be creating its post (an upload
    writes its file before its row commits). `seal` proves that and returns (a lock to hold for
    the whole removal, a DSN and its environment for reading post rows); this module has no
    seal of its own, so remove without one is refused. The node move supplies it
    (kura_move.py orphans-remove)."""
    guard=MountGuard(mount,root,source)
    lock=None
    if remove:
        if seal is None:raise RuntimeError('removing orphans needs a seal proving nothing is creating posts')
        lock,dsn,env=seal()
        connect=lambda:psycopg2.connect(dsn,password=env['PGPASSWORD'])
    else:
        connect=lambda:psycopg2.connect(DATABASE_URL_SYNC)
    try:
        folder=guard.root/'orphans'
        if not folder.exists():folder.mkdir();fsync_dir(guard.root)
        manifest=folder/'manifest.jsonl'
        if not manifest.exists():manifest.touch(0o644);fsync_dir(folder)
        counts={'files':0,'copied':0,'removed':0,'skipped':0}
        with archive_lock(guard),closing(connect()) as pg:
            entries=[e for e in os.scandir(guard.source) if ANY_NAME.fullmatch(e.name) and not e.is_symlink()
                     and e.is_file(follow_symlinks=False)]
            pids=sorted({int(ANY_NAME.fullmatch(e.name)[1]) for e in entries})
            with pg.cursor() as db:
                db.execute('SELECT id FROM post WHERE id=ANY(%s)',(pids,));present={r[0] for r in db}
            pg.rollback()
            copied={}
            with open(manifest,'a') as out:
                for e in sorted(entries,key=lambda e:e.name):
                    pid=int(ANY_NAME.fullmatch(e.name)[1])
                    if pid in present:continue
                    if limit and counts['files']>=limit:break
                    counts['files']+=1;guard.check()
                    st=e.stat(follow_symlinks=False);sig=signature(st)
                    if sig[:2] not in copied or copied[sig[:2]][0]!=sig:
                        # The full signature, not just the inode: a file changed between two of its
                        # names is copied again rather than recorded with the old content's digest.
                        row=dict(dev=sig[0],ino=sig[1],size=sig[2],mtime=sig[3],sha=None,md5=None)
                        target,digest=verified_copy(e.path,guard,row,any_content=True)
                        copied[sig[:2]]=(sig,target,digest);counts['copied']+=1
                    _,target,digest=copied[sig[:2]]
                    out.write(json.dumps(dict(name=e.name,pid=pid,sha256=digest,size=st.st_size,mtime_ns=st.st_mtime_ns,at=_now()))+'\n')
                    out.flush();os.fsync(out.fileno())
                    if not remove:continue
                    with pg:
                        with pg.cursor() as db:
                            # Sealed: no session can insert a post, so an absent row stays absent.
                            db.execute('SELECT 1 FROM post WHERE id=%s',(pid,))
                            if db.fetchone():counts['skipped']+=1;continue
                    with files.content_lock(pid):
                        if signature(os.lstat(e.path))!=sig:counts['skipped']+=1;continue
                        verify_object(target,digest,st.st_size)
                        os.unlink(e.path);fsync_dir(guard.source);counts['removed']+=1
        return counts
    finally:
        if lock is not None:lock.__exit__(None,None,None)


def main():
    ap=argparse.ArgumentParser(description=__doc__);sub=ap.add_subparsers(dest='command',required=True)
    p=sub.add_parser('inventory');p.add_argument('--plan',type=Path,required=True);p.add_argument('--mount',type=Path,required=True);p.add_argument('--archive-root',type=Path,required=True);p.add_argument('--source',type=Path,default=files.storage_directory('content'))
    p=sub.add_parser('run');p.add_argument('--plan',type=Path,help='default: the declared plan');p.add_argument('--workers',type=int,default=8,help='processes checking animations ahead of the serial loop (0: none)');p.add_argument('--limit',type=int,default=500);p.add_argument('--seconds',type=int,default=120)
    p=sub.add_parser('restore');p.add_argument('--plan',type=Path,help='default: the declared plan');p.add_argument('--post-id',type=int,required=True)
    p=sub.add_parser('status');p.add_argument('--plan',type=Path,help='default: the declared plan')
    sub.add_parser('declared',help='the plan tagger/deploy/nodes.json declares for this node, and whether the disk holds it')
    # The archive on another host (see the section above).
    p=sub.add_parser('inventory-remote');p.add_argument('--plan',type=Path,required=True);p.add_argument('--source',type=Path,default=files.storage_directory('content'))
    sub.add_parser('remote-dispatch',help='forced ssh command on the source node; reads SSH_ORIGINAL_COMMAND')
    p=sub.add_parser('orphans',help="files of post ids with no post: into the CAS with a manifest (removal: kura_move.py orphans-remove)")
    p.add_argument('--mount',type=Path,required=True);p.add_argument('--archive-root',type=Path,required=True)
    p.add_argument('--source',type=Path,default=files.storage_directory('content'))
    p.add_argument('--limit',type=int,default=0)
    p=sub.add_parser('run-remote');p.add_argument('--mount',type=Path,required=True);p.add_argument('--archive-root',type=Path,required=True)
    p.add_argument('--source-host',required=True);p.add_argument('--key',type=Path,required=True)
    p.add_argument('--limit',type=int,default=50);p.add_argument('--seconds',type=int,default=120)
    args=ap.parse_args()
    if args.command=='remote-dispatch':
        # The plan is the one tagger/deploy/nodes.json declares for this node; the
        # writing commands re-check it, remote-status reports a missing declaration.
        try:plan=declared_plan()[0]
        except Exception:plan=None
        try:return remote_dispatch(os.environ.get('SSH_ORIGINAL_COMMAND',''),plan)
        except Exception as exc:
            print(f'refused: {exc}',file=sys.stderr);raise SystemExit(3)
    if args.command=='inventory':
        guard=MountGuard(args.mount,args.archive_root,args.source)
        with archive_lock(guard):out=inventory(args.plan,guard)
    elif args.command=='run':out=run_plan(args.plan,args.limit,args.seconds,args.workers)
    elif args.command=='restore':out=restore(args.plan,args.post_id)
    elif args.command=='orphans':out=orphans(args.mount,args.archive_root,args.source,limit=args.limit)
    elif args.command=='inventory-remote':
        from api.config import ARCHIVE_ID
        out=inventory_remote(args.plan,args.source,ORIGINAL_ARCHIVE_ROOT,ARCHIVE_ID)
    elif args.command=='run-remote':
        from api.config import ARCHIVE_ID
        out=run_remote(args.mount,args.archive_root,ssh_transport(args.source_host,args.key),
                       args.limit,args.seconds,expected_archive_id=ARCHIVE_ID or None)
    elif args.command=='declared':out=declared()
    else:out=summary(connect_plan(any_plan(args.plan)))
    print(json.dumps(out,sort_keys=True))
    if args.command=='declared' and not out['matches']:raise SystemExit(1)


if __name__=='__main__':main()
