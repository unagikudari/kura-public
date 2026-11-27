"""One local progress hint; durable outcomes remain in governed registrations."""
import json
import os
from pathlib import Path
import tempfile
import time

STATES={'preparing':'学習用データを準備中','measuring':'学習・評価を実行中',
        'registered':'評価履歴を登録済み','measured':'評価完了・登録待ち','held':'学習条件待ち','stopped':'学習処理が停止'}


def path():
    return Path(os.environ.get('XDG_STATE_HOME') or Path.home()/'.local/state')/'kura-tagger'/'learning-progress.json'


def write(tag,event,state,*,fresh_for_s=90,reason=None):
    if state not in STATES:raise ValueError('unknown progress state')
    target=path();target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,name=tempfile.mkstemp(prefix='.learning-progress-',dir=target.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(dict(schema='kura-learning-progress/1',tag=tag,event=event,state=state,
                           observed_at=time.time(),fresh_for_s=fresh_for_s,reason=reason),stream)
            stream.flush();os.fsync(stream.fileno())
        os.replace(name,target)
    finally:
        if os.path.exists(name):os.unlink(name)


def read(*,now=None):
    try:
        value=json.loads(path().read_text())
        if (value['schema']!='kura-learning-progress/1' or value['state'] not in STATES
                or not isinstance(value['tag'],str) or type(value['event']) is not int):
            return None
        age=(time.time() if now is None else now)-value['observed_at']
        fresh=0<=age<=min(1200,max(0,value['fresh_for_s']))
        label=STATES[value['state']]
        if not fresh and value['state'] in ('preparing','measuring','measured'):
            label='学習の進捗更新が停止（稼働未確認）'
        return dict(tag=value['tag'],event=value['event'],label=label,fresh=fresh,state=value["state"],observed_at=value["observed_at"],age_seconds=max(0,age))
    except (OSError,ValueError,KeyError,TypeError):
        return None
