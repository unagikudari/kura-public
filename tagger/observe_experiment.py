"""Read one explicit experimental unit; output only bounded observation fields.

Run on the experiment host. No SSH, model loading, credentials, or DB access.
The consumer must reject stale observations; a timer being alive is not evidence
that the experiment is running. This tool does not register or publish models.
"""
import argparse
import json
import re
import subprocess
import time
from pathlib import Path
from tagger.experiment_status import SCHEMA, validate


def project(lines, active, failed, completed, packaged, *, tag, host, experiment):
    phase, done, total = 'stopped', 0, 0
    if failed:
        phase = 'stopped'
    elif packaged:
        phase = 'completed'
    elif active and not failed:
        phase = 'evaluating' if completed else 'extracting'
        if not completed:
            for line in lines:
                try: row = json.loads(line)
                except (ValueError, TypeError): continue
                if not isinstance(row, dict): continue
                if row.get('state') == 'extracting_pre_last_stage':
                    d, t = row.get('done'), row.get('total')
                    if type(d) is int and type(t) is int and 0 <= d <= t <= 100000000:
                        phase, done, total = 'extracting', d, t
                elif row.get('state') == 'training' and row.get('arm') in ('frozen_last_stage', 'trainable_last_stage'):
                    epoch = row.get('epoch')
                    if type(epoch) is int and 1 <= epoch <= 5:
                        phase, done, total = 'training', epoch + (5 if row['arm'] == 'trainable_last_stage' else 0), 10
    result = dict(schema=SCHEMA, tag=tag, host=host, experiment=experiment,
                  phase=phase, done=done, total=total, observed_at=time.time(), published=False)
    validate(result)
    return result


def command(argv):
    return subprocess.check_output(argv, text=True, stderr=subprocess.DEVNULL, timeout=10)


def result_ok(path):
    if not path.exists(): return False
    if path.is_symlink() or path.stat().st_size > 1000000: raise ValueError('result_shape')
    return json.loads(path.read_text()).get('outcome') == 'succeeded'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--training-unit', required=True)
    parser.add_argument('--finish-unit', required=True)
    parser.add_argument('--result-dir', type=Path, required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--host', required=True)
    parser.add_argument('--experiment', required=True)
    args = parser.parse_args()
    for unit in (args.training_unit, args.finish_unit):
        if not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', unit): raise ValueError('unit_shape')
    states = [command(['systemctl', '--user', 'show', unit, '-p', 'ActiveState', '--value']).strip()
              for unit in (args.training_unit, args.finish_unit)]
    lines = command(['journalctl', '--user', '-u', args.training_unit, '-n', '30', '--no-pager', '-o', 'cat']).splitlines()
    root = args.result_dir
    completed = result_ok(root / 'result.json')
    active = states[1 if completed else 0] == 'active'
    value = project(lines, active, (root / 'failed.json').exists() or (root / 'packaged/failed.json').exists(),
                    completed, result_ok(root / 'packaged/result.json'),
                    tag=args.tag, host=args.host, experiment=args.experiment)
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    try: main()
    except Exception:
        # Exception text can contain paths or arbitrary subprocess output.
        print(json.dumps({'outcome': 'refused', 'state': 'observation',
                          'reason': 'experiment_observation_failed', 'next': 'inspect_observer_locally'}))
        raise SystemExit(1)
