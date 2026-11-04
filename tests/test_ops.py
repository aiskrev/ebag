"""Operational behaviour found by running the real stack: locks, slots, alerts."""
import copy
import gzip
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from ebag import cli
from ebag.core import prepare
from ebag.crawler import AccessDenied, Client, PolicyError, ReviewRequired
from ebag.db import Repository
from ebag.notify import deliver, operator_alert, pending, resolve
from ebag.ops import Busy, exclusive, get_slot, put_slot, review_key, rotate_backup, tick

C = json.loads(Path('config/preferences.yaml').read_text())
FIXTURE = json.loads(Path('tests/fixtures/ebag-offers-20261024.json').read_text())['results']
ZONE = ZoneInfo('Europe/Sofia')
AT_0900 = datetime(2026, 11, 4, 9, 0, tzinfo=ZONE)
DEAL = dict(excluded=False, real_discount=30, classification='BUY', score=80, confidence='HIGH', quantity='BUY 1', watchlist=False, flags=[])


def product():
    return prepare(dict(name='Яйца 10 бр', external_id='TEST', price=2, category='eggs', availability='in_stock', observed_at='2026-10-24T08:00:00+00:00'))


class TempRepo(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = self.folder.name + '/db.sqlite3'
        self.repo = Repository(self.db)
        self.addCleanup(self.repo.conn.close)


class Slots(TempRepo):
    def status(self):
        return get_slot(self.repo, '2026-11-04')['status']

    def test_waits_for_configured_time(self):
        early = datetime(2026, 11, 4, 7, 59, tzinfo=ZONE)
        self.assertIsNone(tick(self.repo, C, lambda: self.fail('must not run'), early))

    def test_success_runs_once_per_day(self):
        calls = []
        self.assertEqual(tick(self.repo, C, lambda: calls.append(1) or {'status': 'success'}, AT_0900), 'success')
        self.assertIsNone(tick(self.repo, C, lambda: calls.append(1) or {'status': 'success'}, AT_0900))
        self.assertEqual(len(calls), 1)

    def test_transient_failure_retries_later_then_gives_up(self):
        alerts = []

        def boom():
            raise OSError('network down')
        alert = lambda kind, detail: alerts.append(kind)
        self.assertEqual(tick(self.repo, C, boom, AT_0900, alert), 'retry')
        self.assertIsNone(tick(self.repo, C, boom, AT_0900, alert), 'backoff not honoured')
        self.assertEqual(tick(self.repo, C, boom, AT_0900.replace(hour=10, minute=1), alert), 'retry')
        self.assertEqual(alerts, [], 'no alert while retries remain')
        self.assertEqual(tick(self.repo, C, boom, AT_0900.replace(hour=12, minute=1), alert), 'failed')
        self.assertEqual(alerts, ['crawl_failed'])
        self.assertIsNone(tick(self.repo, C, boom, AT_0900.replace(hour=23), alert))

    def test_store_refusal_is_never_retried_same_day(self):
        alerts = []

        def refused():
            raise AccessDenied('Access/rate limit 429; stopping')
        self.assertEqual(tick(self.repo, C, refused, AT_0900, lambda k, d: alerts.append(k)), 'blocked_access')
        self.assertIsNone(tick(self.repo, C, refused, AT_0900.replace(hour=23)))
        self.assertEqual(alerts, ['access_denied'])

    def test_policy_stop_is_not_retried(self):
        def drift():
            raise PolicyError('Listing schema drift')
        self.assertEqual(tick(self.repo, C, drift, AT_0900), 'failed')
        self.assertIsNone(tick(self.repo, C, drift, AT_0900.replace(hour=23)))

    def test_review_block_clears_only_after_recorded_review(self):
        def gate():
            raise ReviewRequired('Storefront release changed')
        self.assertEqual(tick(self.repo, C, gate, AT_0900), 'blocked_review')
        self.assertIsNone(tick(self.repo, C, gate, AT_0900.replace(hour=11)), 'restart without review must not re-run')
        reviewed = copy.deepcopy(C)
        reviewed['crawl']['reviewed_store_commit'] = 'f' * 40
        self.assertNotEqual(review_key(C), review_key(reviewed))
        self.assertEqual(tick(self.repo, reviewed, lambda: {'status': 'success'}, AT_0900.replace(hour=11)), 'success')

    def test_manual_run_in_progress_does_not_consume_attempt(self):
        def busy():
            raise Busy('lock')
        self.assertEqual(tick(self.repo, C, busy, AT_0900), 'busy')
        self.assertIsNone(get_slot(self.repo, '2026-11-04'))
        self.assertEqual(tick(self.repo, C, lambda: {'status': 'success'}, AT_0900), 'success')

    def test_process_death_mid_run_is_retried(self):
        put_slot(self.repo, '2026-11-04', 'started', 1)
        self.assertEqual(tick(self.repo, C, lambda: {'status': 'success'}, AT_0900), 'success')

    def test_partial_run_raises_alert_without_retry(self):
        alerts = []
        self.assertEqual(tick(self.repo, C, lambda: {'status': 'partial', 'errors': 2}, AT_0900, lambda k, d: alerts.append(k)), 'partial')
        self.assertEqual(alerts, ['parser_errors'])

    def test_v1_slot_table_is_migrated(self):
        path = self.folder.name + '/old.sqlite3'
        old = sqlite3.connect(path)
        old.execute('CREATE TABLE scheduled_slots(day TEXT PRIMARY KEY,status TEXT)')
        old.execute("INSERT INTO scheduled_slots VALUES('2026-11-04','failed')")
        old.commit()
        old.close()
        repo = Repository(path)
        self.addCleanup(repo.conn.close)
        self.assertEqual(get_slot(repo, '2026-11-04')['attempts'], 0)
        self.assertEqual(repo.conn.execute('SELECT max(version) FROM schema_migrations').fetchone()[0], 2)


class Locks(unittest.TestCase):
    def test_lock_excludes_second_holder_and_is_released(self):
        with tempfile.TemporaryDirectory() as td:
            with exclusive(td + '/l'):
                with self.assertRaises(Busy):
                    with exclusive(td + '/l'):
                        pass
            with exclusive(td + '/l'):
                pass

    def test_operator_commands_work_while_scheduler_lock_is_held(self):
        """The bug found in Docker: smtp-test/report/run failed behind the scheduler."""
        with tempfile.TemporaryDirectory() as td:
            db = td + '/db.sqlite3'
            Repository(db).conn.close()
            base = [sys.executable, '-m', 'ebag.cli']
            with exclusive(db + '.scheduler.lock'):
                result = subprocess.run(base + ['report', '--db', db, '--output', td + '/r.json'], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = subprocess.run(base + ['run', '--db', db, '--import-file', 'tests/fixtures/products.json', '--output', td + '/r.json'], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            with exclusive(db + '.lock'):
                result = subprocess.run(base + ['run', '--db', db, '--import-file', 'tests/fixtures/products.json', '--output', td + '/r.json'], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, 'two crawls must still exclude each other')


class FakeClient:
    """Stands in for the network; everything else in cli.run is the real path."""
    records = []

    def __init__(self, c):
        self.stats = {'listing_pages': 1, 'listing_truncated': False, 'tracked_pending': 0, 'missing_products': [], 'parse_errors': [], 'parse_error_count': 0, 'release_drift': None}

    def collect(self, tracked):
        return list(self.records)

    def save_state(self):
        FakeClient.frontier_saved = True


class RunFlow(TempRepo):
    def setUp(self):
        super().setUp()
        FakeClient.frontier_saved = False
        self.output = self.folder.name + '/report.json'
        patcher = patch.object(cli, 'Client', FakeClient)
        patcher.start()
        self.addCleanup(patcher.stop)
        muted = patch('builtins.print')
        muted.start()
        self.addCleanup(muted.stop)

    def records(self, count=3):
        from ebag.ebag_source import adapt
        return [adapt(row) for row in FIXTURE[:count]]

    def last_run(self):
        repo = Repository(self.db)
        self.addCleanup(repo.conn.close)
        return repo.conn.execute('SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1').fetchone()

    def test_email_failure_does_not_fail_the_crawl(self):
        FakeClient.records = self.records()
        with patch.object(cli, 'deliver', side_effect=RuntimeError('Configure SMTP_HOST and SMTP_FROM in .env')):
            result = cli.run(C, self.db, send=True, output=self.output)
        self.assertEqual(result['status'], 'success')
        self.assertIn('SMTP_HOST', result['email_error'])
        self.assertTrue(FakeClient.frontier_saved, 'frontier must advance even if mail is down')
        run = self.last_run()
        self.assertEqual(run['status'], 'success')
        self.assertEqual(json.loads(run['details_json'])['source'], 'live')

    def test_isolated_bad_record_keeps_observations_and_blocks_email(self):
        bad = dict(self.records(1)[0], price='not a number', external_id='BAD')
        FakeClient.records = self.records() + [bad]
        with patch.object(cli, 'deliver') as deliver_mock:
            result = cli.run(C, self.db, send=True, output=self.output)
        deliver_mock.assert_not_called()
        self.assertEqual((result['status'], result['errors'], result['products']), ('partial', 1, 3))
        repo = Repository(self.db)
        self.addCleanup(repo.conn.close)
        self.assertEqual(repo.conn.execute('SELECT count(*) FROM price_history').fetchone()[0], 3)

    def test_widespread_parse_failure_is_rejected_and_rolled_back(self):
        good = self.records(1)
        FakeClient.records = good + [dict(good[0], price='x', external_id=f'BAD{i}') for i in range(5)]
        with self.assertRaises(RuntimeError):
            cli.run(C, self.db, output=self.output)
        self.assertEqual(self.last_run()['status'], 'failed')
        repo = Repository(self.db)
        self.addCleanup(repo.conn.close)
        self.assertEqual(repo.conn.execute('SELECT count(*) FROM price_history').fetchone()[0], 0)

    def test_import_cannot_send_and_leaves_no_trace(self):
        with self.assertRaises(RuntimeError):
            cli.run(C, self.db, 'tests/fixtures/products.json', True, self.output)
        self.assertEqual(self.repo.conn.execute('SELECT count(*) FROM crawl_runs').fetchone()[0], 0)


class Health(TempRepo):
    def finish_live(self, status='success', **detail):
        run = self.repo.start()
        self.repo.finish(run, status, 1, 1, 0, dict(source='live', **detail))
        return run

    def test_crawl_in_progress_is_not_a_failure(self):
        self.finish_live()
        self.repo.start()
        report = cli.health(self.repo)
        self.assertTrue(report['live_crawler_healthy'])
        self.assertEqual(report['last_run_status'], 'success')

    def test_failure_is_visible_but_recent_success_still_counts(self):
        self.finish_live()
        run = self.repo.start()
        self.repo.finish(run, 'failed', 0, 0, 0, {'error': 'Storefront release changed'})
        report = cli.health(self.repo)
        self.assertTrue(report['live_crawler_healthy'])
        self.assertEqual(report['last_error'], 'Storefront release changed')

    def test_import_never_makes_live_health_green(self):
        run = self.repo.start()
        self.repo.finish(run, 'success', 1, 1, 0, {'source': 'import'})
        self.assertFalse(cli.health(self.repo)['live_crawler_healthy'])

    def test_stale_success_is_unhealthy(self):
        self.finish_live()
        self.repo.conn.execute("UPDATE crawl_runs SET finished_at='2020-01-01T00:00:00+00:00'")
        self.assertFalse(cli.health(self.repo)['live_crawler_healthy'])


class Provider:
    def __init__(self):
        self.messages = []

    def send(self, recipient, subject, text, markup):
        self.messages.append((recipient, subject, text))


class Alerts(TempRepo):
    def test_alert_once_per_kind_per_day(self):
        provider = Provider()
        with patch.dict(os.environ, {'NOTIFICATION_EMAIL': 'aiskrev@abv.bg'}):
            self.assertEqual(operator_alert(self.repo, C, provider, '2026-11-04', 'review_required', 'x'), 1)
            self.assertEqual(operator_alert(self.repo, C, provider, '2026-11-04', 'review_required', 'x'), 0)
            self.assertEqual(operator_alert(self.repo, C, provider, '2026-11-05', 'review_required', 'x'), 1)
        self.assertEqual(len(provider.messages), 2)
        self.assertIn('преглед', provider.messages[0][1])

    def test_alert_failure_never_raises(self):
        class Broken:
            def validate(self):
                raise RuntimeError('Configure SMTP_HOST and SMTP_FROM in .env')
        with patch('sys.stderr'):
            self.assertEqual(operator_alert(self.repo, C, Broken(), '2026-11-04', 'crawl_failed', 'x'), 0)

    def test_alert_does_not_affect_deal_cooldown(self):
        provider = Provider()
        prod = product()
        run = self.repo.start()
        pid, _ = self.repo.save(prod, run)
        self.repo.conn.commit()
        with patch.dict(os.environ, {'NOTIFICATION_EMAIL': 'aiskrev@abv.bg'}):
            operator_alert(self.repo, C, provider, '2026-11-04', 'crawl_failed', 'x')
            self.assertEqual(deliver(self.repo, [(pid, prod, DEAL)], C, provider), 1)


class Reconcile(TempRepo):
    def uncertain(self):
        class Broken:
            def send(self, *args):
                raise RuntimeError('ambiguous transport result')
        prod = product()
        run = self.repo.start()
        pid, _ = self.repo.save(prod, run)
        self.repo.conn.commit()
        with self.assertRaises(RuntimeError):
            deliver(self.repo, [(pid, prod, DEAL)], C, Broken())
        return pid, prod, pending(self.repo)[0]['id']

    def test_failed_verdict_allows_exactly_one_resend(self):
        with patch.dict(os.environ, {'NOTIFICATION_EMAIL': 'aiskrev@abv.bg'}):
            pid, prod, nid = self.uncertain()
            provider = Provider()
            self.assertEqual(deliver(self.repo, [(pid, prod, DEAL)], C, provider), 0, 'blocked until reviewed')
            self.assertEqual(resolve(self.repo, nid, 'failed'), 1)
            self.assertEqual(deliver(self.repo, [(pid, prod, DEAL)], C, provider), 1)
            self.assertEqual(deliver(self.repo, [(pid, prod, DEAL)], C, provider), 0)
        self.assertEqual(self.repo.conn.execute('SELECT status FROM notifications').fetchone()[0], 'sent')

    def test_sent_verdict_never_resends(self):
        with patch.dict(os.environ, {'NOTIFICATION_EMAIL': 'aiskrev@abv.bg'}):
            pid, prod, nid = self.uncertain()
            self.assertEqual(resolve(self.repo, nid, 'sent'), 1)
            provider = Provider()
            self.assertEqual(deliver(self.repo, [(pid, prod, DEAL)], C, provider), 0)
        self.assertEqual(pending(self.repo), [])

    def test_only_pending_rows_can_change(self):
        with patch.dict(os.environ, {'NOTIFICATION_EMAIL': 'aiskrev@abv.bg'}):
            _, _, nid = self.uncertain()
        self.assertEqual(resolve(self.repo, nid, 'sent'), 1)
        self.assertEqual(resolve(self.repo, nid, 'failed'), 0)


class Backups(TempRepo):
    def test_rotation_keeps_newest_and_restores(self):
        self.repo.start()
        self.repo.conn.commit()
        folder = self.folder.name + '/backups'
        for day in ('2026-11-01', '2026-11-02', '2026-11-03'):
            target = rotate_backup(self.repo.conn, folder, 2, day)
        self.assertEqual(sorted(p.name for p in Path(folder).iterdir()), ['ebag-2026-11-02.sqlite3.gz', 'ebag-2026-11-03.sqlite3.gz'])
        restored = self.folder.name + '/restored.sqlite3'
        Path(restored).write_bytes(gzip.decompress(target.read_bytes()))
        copy_db = sqlite3.connect(restored)
        self.addCleanup(copy_db.close)
        self.assertEqual(copy_db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(copy_db.execute('SELECT count(*) FROM crawl_runs').fetchone()[0], 1)


class Crawler(unittest.TestCase):
    """Client against canned responses: the gate and tolerance logic, no network."""

    def client(self, td, terms_commit, **crawl):
        c = copy.deepcopy(C)
        c['crawl'].update(policy_reviewed_at=datetime.now().date().isoformat(), category_seeds=[], max_pages=1, **crawl)
        client = Client(c, td + '/cache')
        pages = {'/robots.txt': 'User-agent: *\nDisallow: /nothing\n', '/terms': f'<script>window.STORE_COMMIT_HASH = "{terms_commit}";</script>'}

        def fake_get(url, policy=False, fresh=False):
            if url.endswith('/terms'):
                self.fresh = fresh
            for path, body in pages.items():
                if url.endswith(path):
                    return body
            return json.dumps({'results': self.rows, 'next': None})
        client.get = fake_get
        return client

    def test_release_change_stops_by_default(self):
        self.rows = FIXTURE[:2]
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ReviewRequired):
                self.client(td, 'abc123').collect()

    def test_release_check_bypasses_cache(self):
        self.rows = FIXTURE[:2]
        with tempfile.TemporaryDirectory() as td:
            self.client(td, C['crawl']['reviewed_store_commit']).collect()
        self.assertTrue(self.fresh)

    def test_warn_mode_continues_and_reports_drift(self):
        self.rows = FIXTURE[:2]
        with tempfile.TemporaryDirectory() as td:
            client = self.client(td, 'abc123', on_release_change='warn')
            self.assertEqual(len(client.collect()), 2)
        self.assertEqual(client.stats['release_drift'], 'abc123')

    def test_warn_mode_still_honours_review_expiry(self):
        self.rows = FIXTURE[:2]
        with tempfile.TemporaryDirectory() as td:
            client = self.client(td, 'abc123', on_release_change='warn')
            client.c['policy_reviewed_at'] = '2020-01-01'
            with self.assertRaises(ReviewRequired):
                client.collect()

    def test_one_bad_listing_row_is_tolerated_and_counted(self):
        broken = dict(FIXTURE[0])
        del broken['current_price_eur']
        self.rows = FIXTURE[1:20] + [broken]
        with tempfile.TemporaryDirectory() as td:
            client = self.client(td, C['crawl']['reviewed_store_commit'])
            self.assertEqual(len(client.collect()), 19)
        self.assertEqual(client.stats['parse_error_count'], 1)

    def test_schema_drift_across_many_rows_stops(self):
        self.rows = []
        for row in FIXTURE[:20]:
            row = dict(row)
            del row['current_price_eur']
            self.rows.append(row)
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(PolicyError):
                self.client(td, C['crawl']['reviewed_store_commit']).collect()


class Configuration(unittest.TestCase):
    def check(self, mutate):
        from ebag.settings import load
        c = copy.deepcopy(C)
        mutate(c)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td + '/c.json')
            path.write_text(json.dumps(c))
            return load(path)

    def test_release_policy_values(self):
        self.assertTrue(self.check(lambda c: c['crawl'].update(on_release_change='warn')))
        with self.assertRaises(ValueError):
            self.check(lambda c: c['crawl'].update(on_release_change='ignore'))

    def test_schedule_and_backup_limits(self):
        with self.assertRaises(ValueError):
            self.check(lambda c: c.update(schedule={'max_attempts': 0}))
        with self.assertRaises(ValueError):
            self.check(lambda c: c.update(backup={'keep': 0}))
