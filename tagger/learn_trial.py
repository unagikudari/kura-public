"""One explicitly selected queued event, local evidence only; never promote/register.

Preparation does not install a timer. Run only after approving the tag/event and
external systemd resource limits described in docs/notes/tagger-learn-trial.md.
"""
from __future__ import annotations
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from sqlalchemy import text
from tagger import learn
from tagger.reconciler import QUEUED_TRAINING_SQL


async def eligible(db, tag, event):
    state = (await db.execute(text('SELECT paused FROM tagger_reconciler_state WHERE id'))).scalar_one_or_none()
    if state is not False:
        return False
    # Candidate disposition and lifecycle stage are different state machines.
    # The candidate CHECK has no 'training'; only the latest unconsumed event
    # in QUEUED_TRAINING_SQL establishes that this exact run is queued.
    candidate_ok = (await db.execute(text("""
      SELECT EXISTS(SELECT 1 FROM tagger_head_candidate WHERE tag=:tag AND status='candidate')
         AND NOT EXISTS(SELECT 1 FROM tagger_head_candidate WHERE tag=:tag AND status='held')
    """), {'tag': tag})).scalar_one()
    if not candidate_ok:
        return False
    return any(r.tag == tag and r.event_id == event for r in (await db.execute(text(QUEUED_TRAINING_SQL))).all())


def run_child(args, directory):
    env = learn.child_env(dict(os.environ))
    env.update(VNEXT_STATE_DIR=str(directory/'state'), VNEXT_CACHE_DIR=str(directory/'cache'),
               KURA_ARTIFACT_DIR=str(directory/'artifacts'), VNEXT_DEV='cpu',
               OMP_NUM_THREADS=str(args.threads), MKL_NUM_THREADS=str(args.threads),
               OPENBLAS_NUM_THREADS=str(args.threads))
    # Direct child SQL is read-only; the parent's bounded lease is separate.
    env['PGOPTIONS'] = env.get('PGOPTIONS', '') + ' -c default_transaction_read_only=on'
    # Probe in the exact measurement interpreter/env, never the API Python.
    preflight_cmd = [args.tagger_python, '-m', 'tagger.learn_preflight',
                     '--tag', args.tag, '--event', str(args.event)]
    try:
        with (directory/'preflight.stdout').open('x') as stdout, \
             (directory/'preflight.stderr').open('x') as stderr:
            preflight = subprocess.run(preflight_cmd, cwd=REPO, env=env,
                                       stdout=stdout, stderr=stderr, timeout=60)
        if preflight.returncode != 0:
            return {'returncode': preflight.returncode, 'timed_out': False,
                    'output': None, 'preflight_failed': True}
    except subprocess.TimeoutExpired:
        return {'returncode': 1, 'timed_out': False, 'output': None,
                'preflight_failed': True, 'preflight_timed_out': True}
    cmd = [args.tagger_python, '-m', 'tagger.vnext_runner', '--tag', args.tag,
           '--event', str(args.event), '--review-only']
    with (directory/'stdout.log').open('x') as stdout, (directory/'stderr.log').open('x') as stderr:
        proc = subprocess.Popen(cmd, cwd=REPO, env=env, stdout=stdout, stderr=stderr,
                                start_new_session=True)
        timed_out = False
        try:
            proc.wait(timeout=args.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            # Kill the entire dedicated group, including any descendant, also
            # when the leader already exited or the parent was interrupted.
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
    payload = learn.parse_child_output((directory/'stdout.log').read_text())
    return {'returncode': proc.returncode, 'timed_out': timed_out, 'output': payload}


def validate_result(result, tag, event):
    if result['timed_out'] or result['returncode'] != 0 or not isinstance(result['output'], dict):
        return False
    payload = result['output'].get('registration')
    return (isinstance(payload, dict) and payload.get('tag') == tag
            and payload.get('lifecycle_event_id') == event)


async def run_trial(args, directory):
    async with learn.async_session() as db:
        if not await eligible(db, args.tag, args.event):
            raise RuntimeError('exact tag/event is not eligible (paused, held, consumed or moved)')
        await db.rollback()
        lease = await learn.acquire_lease(db, f'lifecycle:{args.tag}',
                                          lease_seconds=args.timeout_seconds + learn.LEASE_MARGIN_S)
        try:
            if not await eligible(db, args.tag, args.event):
                raise RuntimeError('eligibility changed before measurement')
            await db.rollback()
            task = asyncio.create_task(asyncio.to_thread(run_child, args, directory))
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                # Do not release the lifecycle lease while a background child
                # is still measuring. The external cgroup also kills descendants.
                await task
                raise
            still_eligible = await eligible(db, args.tag, args.event)
            result.update(tag=args.tag, event=args.event, still_eligible=still_eligible,
                          reviewable=still_eligible and validate_result(result, args.tag, args.event),
                          registered=False, promoted=False, undeclared_domain='block')
            # Local report only. No registration, auto_review, reconciliation,
            # GT sync, lifecycle transition or backoff retry is called here.
            (directory/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['reviewable'] else 1
        finally:
            await db.rollback()
            await learn.release_lease(db, lease)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--tag', required=True)
    ap.add_argument('--event', type=int, required=True)
    ap.add_argument('--output', type=Path, required=True, help='new private directory, never reused')
    ap.add_argument('--timeout-seconds', type=int, required=True)
    ap.add_argument('--threads', type=int, required=True)
    ap.add_argument('--undeclared-domain', choices=['block'], default='block')
    ap.add_argument('--tagger-python', default=str(Path.home()/'.local/share/kura-tagger/venv/bin/python'))
    args = ap.parse_args()
    if args.event <= 0 or not 1 <= args.threads <= 8 or not 1 <= args.timeout_seconds <= 21600:
        ap.error('require positive event, threads 1..8 and timeout 1..21600 seconds')
    # Share the normal learner's lock, so the two paths cannot run together.
    learn.STATE.mkdir(parents=True, exist_ok=True)
    with (learn.STATE/'learn.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        directory = args.output.resolve()
        directory.mkdir(mode=0o700)  # exclusive, refusal on previous attempt
        (directory/'request.json').write_text(json.dumps({'tag': args.tag, 'event': args.event,
            'timeout_seconds': args.timeout_seconds, 'threads': args.threads,
            'undeclared_domain': 'block', 'register': False, 'promote': False}, indent=2))
        return asyncio.run(run_trial(args, directory))


if __name__ == '__main__':
    sys.exit(main())
