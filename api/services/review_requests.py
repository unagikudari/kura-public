"""Operator requests reference frozen queues; never change queues, labels or quality."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from datetime import datetime


class ReviewRequestRefused(ValueError):
    pass


def require(ok, reason):
    if not ok:
        raise ReviewRequestRefused(reason)


def unique(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, 'duplicate_key')
        value[key] = item
    return value


def load_requests(queues, root=None):
    """None means default directory absent; an explicitly configured missing path refuses."""
    configured = root is not None or 'KURA_REVIEW_REQUEST_DIR' in os.environ
    value = root if root is not None else os.environ.get('KURA_REVIEW_REQUEST_DIR', '~/.local/share/kura/review-requests')
    require(isinstance(value, (str, os.PathLike)) and bool(str(value).strip()), 'directory_path_invalid')
    try:
        root = Path(value).expanduser()
    except (OSError, ValueError, RuntimeError):
        raise ReviewRequestRefused('directory_path_invalid') from None
    require(root.is_absolute(), 'directory_path_invalid')
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        if not configured:
            return None
        raise ReviewRequestRefused('directory_missing') from None
    except OSError:
        raise ReviewRequestRefused('directory_unreadable') from None
    try:
        info = os.fstat(directory)
        require(info.st_uid == os.getuid() and not info.st_mode & 0o022, 'directory_invalid')
        names = sorted(os.listdir(directory))
        require(len(names) <= 20, 'too_many_requests')
        by_sha = {q['queue_sha256']: q for q in queues}
        requests = {}
        for name in names:
            require(re.fullmatch(r'[a-f0-9]{64}\.json', name), 'filename_invalid')
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    info = os.fstat(fd)
                    require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= 4096
                            and info.st_uid == os.getuid() and not info.st_mode & 0o022, 'file_invalid')
                    with os.fdopen(fd, 'rb', closefd=False) as handle:
                        raw = handle.read(4097)
                finally:
                    os.close(fd)
            except OSError:
                raise ReviewRequestRefused('file_unreadable') from None
            require(len(raw) <= 4096, 'file_invalid')
            require(hashlib.sha256(raw).hexdigest()+'.json' == name, 'digest_mismatch')
            try:
                item = json.loads(raw, object_pairs_hook=unique)
            except (ValueError, UnicodeError, RecursionError) as exc:
                if isinstance(exc, ReviewRequestRefused):
                    raise
                raise ReviewRequestRefused('json_invalid') from None
            keys = {'schema','queue_sha256','frame_sha256','reason','evidence_sha256','requested_at'}
            require(isinstance(item, dict) and set(item) == keys, 'shape_invalid')
            require(item['schema'] == 'review-request/1' and item['reason'] == 'class_group_support', 'request_invalid')
            for key in ('queue_sha256','frame_sha256','evidence_sha256'):
                require(isinstance(item[key], str) and re.fullmatch('[a-f0-9]{64}', item[key]), 'hash_invalid')
            when = item['requested_at']
            require(isinstance(when, str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})', when), 'time_invalid')
            try:
                require(datetime.fromisoformat(when).utcoffset() is not None, 'time_invalid')
            except ValueError:
                raise ReviewRequestRefused('time_invalid') from None
            queue = by_sha.get(item['queue_sha256'])
            require(queue is not None and queue['frame_sha256'] == item['frame_sha256'], 'queue_binding_invalid')
            require(queue['schema'] in ('human-review-set/1','human-review-set/2') and queue['purpose'] == 'training_support', 'queue_kind_invalid')
            require(item['queue_sha256'] not in requests, 'duplicate_request')
            requests[item['queue_sha256']] = item
        return requests
    except OSError:
        raise ReviewRequestRefused('directory_unreadable') from None
    finally:
        os.close(directory)
