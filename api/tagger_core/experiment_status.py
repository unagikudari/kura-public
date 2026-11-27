"""Bounded local observation hints, never publication or quality authority."""
import json
import math
import os
from pathlib import Path
import stat
import time

SCHEMA = 'kura-experiment-progress/1'
PHASES = {'extracting': '特徴を抽出中', 'training': '比較学習中',
          'evaluating': '候補を検証中', 'qualifying': 'メモリ・速度を確認中（品質評価とは別）', 'completed': '実験完了・品質判断待ち',
          'stopped': '停止・原因確認が必要'}
LIMIT = 16384


def path():
    return Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'kura-tagger' / 'experiment-progress.json'


def validate(value, *, now=None):
    if not isinstance(value, dict) or value.get('schema') != SCHEMA:
        raise ValueError('experiment_schema')
    result = {'schema': SCHEMA}
    for key in ('tag', 'host', 'experiment'):
        text = value.get(key)
        if not isinstance(text, str) or not 1 <= len(text) <= 120 or any(ord(c) < 32 for c in text):
            raise ValueError('experiment_text')
        result[key] = text
    if value.get('phase') not in PHASES or value.get('published') is not False:
        raise ValueError('experiment_state')
    result['phase'] = value['phase']
    result['published'] = False
    for key in ('done', 'total'):
        number = value.get(key)
        if type(number) is not int or not 0 <= number <= 100000000:
            raise ValueError('experiment_count')
        result[key] = number
    if result['done'] > result['total']:
        raise ValueError('experiment_count')
    observed = value.get('observed_at')
    if type(observed) not in (int, float) or not math.isfinite(observed):
        raise ValueError('experiment_time')
    age = (time.time() if now is None else now) - observed
    result.update(observed_at=observed, fresh=0 <= age <= 120, age_seconds=max(0, age))
    return result


def read(*, now=None):
    try:
        fd = os.open(path(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT or info.st_mode & 0o022:
                return None
            payload = stream.read(LIMIT + 1)
            if len(payload) > LIMIT:
                return None
        return validate(json.loads(payload), now=now)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write(value):
    """Publish a validated observation atomically without refreshing its source time."""
    import tempfile
    safe = validate(value)
    safe = {k: v for k, v in safe.items() if k not in ('fresh', 'age_seconds')}
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.experiment-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(safe, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)
