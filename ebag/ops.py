"""Operational layer: run locking, persisted daily slots, retries and backups.

Slot states
  started         a run began; if the process dies here the slot is retried
  retry           transient failure (network, 5xx); retried later the same day
  blocked_review  terms/parser review needed; re-run only after the reviewed
                  values in the config change (i.e. after a real review)
  blocked_access  the store refused (401/403/429/challenge); no retry that day
  failed          attempts exhausted or a policy stop; needs a person
  success/partial finished
"""
import contextlib
import fcntl
import gzip
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .core import now
from .crawler import AccessDenied, PolicyError, ReviewRequired


class Busy(RuntimeError):
    """Another process holds the lock."""


@contextlib.contextmanager
def exclusive(path):
    """Non-blocking process lock. Held only for the duration of the work."""
    with open(path, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy('Another instance holds lock ' + str(path)) from exc
        yield


def review_key(c):
    """Fingerprint of the reviewed values; it changes only when a review is recorded."""
    crawl = c['crawl']
    fields = ('policy_reviewed', 'parser_validated', 'policy_reviewed_at', 'reviewed_store_commit', 'on_release_change')
    return hashlib.sha256(json.dumps([crawl.get(k) for k in fields]).encode()).hexdigest()[:16]


def get_slot(repo, day):
    return repo.conn.execute('SELECT * FROM scheduled_slots WHERE day=?', (day,)).fetchone()


def put_slot(repo, day, status, attempts=0, next_attempt_at=None, key=None, detail=None):
    repo.conn.execute(
        'INSERT INTO scheduled_slots(day,status,attempts,next_attempt_at,review_key,detail) VALUES(?,?,?,?,?,?) '
        'ON CONFLICT(day) DO UPDATE SET status=excluded.status,attempts=excluded.attempts,'
        'next_attempt_at=excluded.next_attempt_at,review_key=excluded.review_key,detail=excluded.detail',
        (day, status, attempts, next_attempt_at, key, detail))
    repo.conn.commit()


def due(row, c, local):
    if row is None:
        return True
    status = row['status']
    if status == 'blocked_review':
        return row['review_key'] != review_key(c)
    if status == 'started':
        return (row['attempts'] or 0) < c.get('schedule', {}).get('max_attempts', 3)
    if status == 'retry':
        return local >= datetime.fromisoformat(row['next_attempt_at'])
    return False


def tick(repo, c, execute, local, alert=lambda kind, detail: None):
    """Run today's slot if it is due. Returns the new slot status, or None if idle."""
    if local.strftime('%H:%M') < c['daily_crawl_time']:
        return None
    day = local.date().isoformat()
    row = get_slot(repo, day)
    if not due(row, c, local):
        return None
    limits = c.get('schedule', {})
    attempts = (row['attempts'] or 0) + 1 if row else 1
    put_slot(repo, day, 'started', attempts)
    try:
        result = execute()
    except Busy:
        # A manual run owns the lock; this does not count as an attempt.
        if row:
            put_slot(repo, day, row['status'], row['attempts'] or 0, row['next_attempt_at'], row['review_key'], row['detail'])
        else:
            repo.conn.execute('DELETE FROM scheduled_slots WHERE day=?', (day,))
            repo.conn.commit()
        return 'busy'
    except ReviewRequired as exc:
        put_slot(repo, day, 'blocked_review', attempts, key=review_key(c), detail=str(exc))
        alert('review_required', str(exc))
        return log_failure('blocked_review', exc)
    except AccessDenied as exc:
        put_slot(repo, day, 'blocked_access', attempts, detail=str(exc))
        alert('access_denied', str(exc))
        return log_failure('blocked_access', exc)
    except PolicyError as exc:
        put_slot(repo, day, 'failed', attempts, detail=str(exc))
        alert('policy_stop', str(exc))
        return log_failure('failed', exc)
    except Exception as exc:
        if attempts < limits.get('max_attempts', 3):
            wait = timedelta(minutes=limits.get('retry_minutes', 60) * attempts)
            put_slot(repo, day, 'retry', attempts, (local + wait).isoformat(), detail=str(exc))
            return log_failure('retry', exc)
        put_slot(repo, day, 'failed', attempts, detail=str(exc))
        alert('crawl_failed', str(exc))
        return log_failure('failed', exc)
    status = result.get('status', 'success')
    put_slot(repo, day, status, attempts, detail=result.get('email_error'))
    if status == 'partial':
        alert('parser_errors', f"{result.get('errors')} products failed to parse; email was suppressed")
    if result.get('email_error'):
        print('Email delivery failed: ' + result['email_error'], file=sys.stderr, flush=True)
    return status


def log_failure(status, exc):
    print(f'{now()} slot {status}: {exc}', file=sys.stderr, flush=True)
    return status


def rotate_backup(conn, directory, keep, day):
    """Write a compressed daily online backup and keep the newest `keep` files."""
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f'ebag-{day}.sqlite3.gz'
    temp = folder / f'.ebag-{day}.tmp'
    packed = folder / f'.ebag-{day}.gz.tmp'
    try:
        dst = sqlite3.connect(temp)
        try:
            conn.backup(dst)
        finally:
            dst.close()
        with open(temp, 'rb') as src, gzip.open(packed, 'wb') as out:
            shutil.copyfileobj(src, out)
        packed.replace(target)
    finally:
        temp.unlink(missing_ok=True)
        packed.unlink(missing_ok=True)
    for old in sorted(folder.glob('ebag-*.sqlite3.gz'))[:-keep]:
        old.unlink()
    return target
