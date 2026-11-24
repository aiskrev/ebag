"""The /offers crawl against a scripted HTTP layer. No real network.

Covers the 404 incident of 08.11 and 21-23.11: a page number stored from an
earlier day (categories/418/products/json?page=6) no longer existed.
"""
import copy
import json
import tempfile
import unittest
import urllib.error
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from ebag import cli
from ebag.crawler import (AccessDenied, Client, FetchError, PolicyError, dedupe_key, is_offers_page, is_product_url,
                          offers_url)
from ebag.db import Repository
from ebag.ops import get_slot, tick

C = json.loads(Path('config/preferences.yaml').read_text())
ROWS = json.loads(Path('tests/fixtures/ebag-offers-20261024.json').read_text())['results']
COMMIT = C['crawl']['reviewed_store_commit']
PAGE1 = offers_url(1)
PAGE2 = offers_url(2)


class Response:
    def __init__(self, body, status=200):
        self.body = body.encode() if isinstance(body, str) else body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self.body


class Opener:
    """url -> list of outcomes, consumed in order. Records every call."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    def open(self, request, timeout):
        url = request.full_url
        self.calls.append((url, timeout))
        outcome = self.script[url].pop(0) if len(self.script.get(url, [])) > 1 else self.script[url][0]
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, int):
            raise urllib.error.HTTPError(url, outcome, 'status', {}, None)
        if isinstance(outcome, tuple):
            code, headers = outcome
            raise urllib.error.HTTPError(url, code, 'status', headers, None)
        return Response(outcome)


def page(rows, next_url=None, count=None):
    return json.dumps({'count': len(rows) if count is None else count, 'next': next_url, 'previous': None, 'results': rows})


def policy(commit=COMMIT):
    return {'https://ebag.bg/robots.txt': ['User-agent: *\nDisallow: */search/?*sortBy\n'],
            'https://ebag.bg/terms': [f'<script>window.STORE_COMMIT_HASH = "{commit}";</script>']}


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = folder.name
        for target in ('ebag.crawler.time.sleep',):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = copy.deepcopy(C)
        self.config['crawl']['policy_reviewed_at'] = date.today().isoformat()

    def client(self, script, **crawl):
        self.config['crawl'].update(crawl)
        client = Client(self.config, self.dir + '/cache')
        client.opener = Opener({**policy(), **script})
        return client

    def requested(self, client):
        return [url for url, _ in client.opener.calls]


class Offers(Base):
    def test_1_success_follows_pagination_and_marks_provenance(self):
        client = self.client({PAGE1: [page(ROWS[:10], PAGE2, count=20)], PAGE2: [page(ROWS[10:20])]})
        products = client.collect()
        self.assertEqual(len(products), 20)
        self.assertTrue(all(p['source'] == 'offers' and is_offers_page(p['source_url']) for p in products))
        self.assertEqual(client.stats['offers_count'], 20)
        self.assertEqual(client.stats['listing_pages'], 2)
        self.assertEqual(client.stats['warnings'], [])

    def test_2_first_offers_page_404_fails_once_without_retry(self):
        client = self.client({PAGE1: [404]})
        with self.assertRaises(FetchError) as caught:
            client.collect()
        diag = caught.exception.diag
        self.assertEqual((diag['status'], diag['url'], diag['method'], diag['stage']), (404, PAGE1, 'GET', 'оферти, страница 1'))
        self.assertFalse(caught.exception.transient)
        self.assertEqual(self.requested(client).count(PAGE1), 1, '404 must not be retried')

    def test_3_pagination_404_keeps_what_was_found(self):
        """The list shrank between page 1 and page 2: not a reason to lose the day."""
        client = self.client({PAGE1: [page(ROWS[:10], PAGE2, count=20)], PAGE2: [404]})
        products = client.collect()
        self.assertEqual(len(products), 10)
        self.assertEqual(client.stats['pagination_stopped'], {'url': PAGE2, 'status': 404})

    def test_3b_invalid_product_url_is_skipped_not_fatal(self):
        bad = dict(ROWS[0], url_slug='../../evil')
        client = self.client({PAGE1: [page([bad] + ROWS[1:5])]})
        self.assertEqual(len(client.collect()), 4)
        self.assertEqual(client.stats['invalid_urls'], 1)

    def test_4_rate_limit_stops_with_retry_after(self):
        client = self.client({PAGE1: [(429, {'Retry-After': '120'})]})
        with self.assertRaises(AccessDenied) as caught:
            client.collect()
        self.assertEqual(caught.exception.diag['retry_after'], '120')
        self.assertIn('Retry-After: 120', str(caught.exception))
        self.assertEqual(self.requested(client).count(PAGE1), 1, 'a rate limit is respected, never retried at once')

    def test_5_temporary_500_then_success(self):
        client = self.client({PAGE1: [500, page(ROWS[:3])]})
        self.assertEqual(len(client.collect()), 3)
        self.assertEqual(self.requested(client).count(PAGE1), 2)

    def test_5b_persistent_503_is_transient_after_three_attempts(self):
        client = self.client({PAGE1: [503]})
        with self.assertRaises(FetchError) as caught:
            client.collect()
        self.assertTrue(caught.exception.transient)
        self.assertEqual(caught.exception.diag['attempts'], 3)

    def test_6_timeouts_are_retried_then_reported(self):
        client = self.client({PAGE1: [TimeoutError('read timed out')]})
        with self.assertRaises(FetchError) as caught:
            client.collect()
        self.assertTrue(caught.exception.transient)
        self.assertEqual(self.requested(client).count(PAGE1), 3)
        self.assertEqual(caught.exception.diag['timeout'], self.config['crawl']['timeout_seconds'])
        client = self.client({PAGE1: [urllib.error.URLError(TimeoutError('connect')), page(ROWS[:2])]})
        self.assertEqual(len(client.collect()), 2)

    def test_7_same_host_redirect_is_followed_and_recorded(self):
        moved = 'https://ebag.bg/categories/offers-products/json?page=1&mode=all_offers&v=2'
        client = self.client({PAGE1: [(301, {'Location': moved})], moved: [page(ROWS[:2])]})
        text = client.get(PAGE1, kind='offers_page') if client.check_policy() is None else None
        self.assertEqual(len(json.loads(text)['results']), 2)
        self.assertEqual(self.requested(client)[-2:], [PAGE1, moved])

    def test_7b_redirect_outside_ebag_is_refused(self):
        client = self.client({PAGE1: [(302, {'Location': 'https://evil.example/offers'})]})
        with self.assertRaises(PolicyError) as caught:
            client.collect()
        self.assertIn('outside ebag.bg', str(caught.exception))
        self.assertNotIn('https://evil.example/offers', self.requested(client))

    def test_8_relative_next_link(self):
        client = self.client({PAGE1: [page(ROWS[:2], '/categories/offers-products/json?page=2&mode=all_offers')], PAGE2: [page(ROWS[2:4])]})
        self.assertEqual(len(client.collect()), 4)
        self.assertIn(PAGE2, self.requested(client))

    def test_9_absolute_next_link(self):
        absolute = 'https://ebag.bg/categories/offers-products/json?mode=all_offers&page=2'
        client = self.client({PAGE1: [page(ROWS[:2], absolute)], absolute: [page(ROWS[2:4])]})
        self.assertEqual(len(client.collect()), 4)

    def test_10_duplicates_are_removed_by_product_id(self):
        client = self.client({PAGE1: [page(ROWS[:3], PAGE2)], PAGE2: [page(ROWS[2:5])]})
        products = client.collect()
        self.assertEqual(len(products), 5)
        self.assertEqual(len({p['external_id'] for p in products}), 5)
        self.assertEqual(client.stats['duplicates'], 1)

    def test_10b_dedupe_key_priority(self):
        self.assertEqual(dedupe_key({'external_id': '7', 'url': 'u'}), 'id:7')
        self.assertEqual(dedupe_key({'url': 'https://ebag.bg/x/1'}), 'url:https://ebag.bg/x/1')
        self.assertEqual(dedupe_key({'name': ' Мляко ', 'package': '1 л', 'price': 1.5}), dedupe_key({'name': 'мляко', 'package': '1 Л', 'price': 1.5}))

    def test_12_missing_current_price_is_counted_not_fatal(self):
        broken = dict(ROWS[0])
        broken['prices_data_per_currency'] = {'EUR': {}}
        client = self.client({PAGE1: [page([broken] + ROWS[1:20])]})
        self.assertEqual(len(client.collect()), 19)
        self.assertEqual(client.stats['parse_error_count'], 1)

    def test_18_empty_offers_is_a_failure(self):
        client = self.client({PAGE1: [page([])]})
        with self.assertRaises(PolicyError):
            client.collect()

    def test_19_parser_regression_html_instead_of_json(self):
        client = self.client({PAGE1: ['<!DOCTYPE html><html><body>Нова страница</body></html>']})
        with self.assertRaises(PolicyError) as caught:
            client.collect()
        self.assertIn('not JSON', str(caught.exception))

    def test_19b_parser_regression_missing_results(self):
        client = self.client({PAGE1: [json.dumps({'items': []})]})
        with self.assertRaises(PolicyError):
            client.collect()

    def test_19c_most_rows_unparseable_stops_as_drift(self):
        rows = [{k: v for k, v in r.items() if k != 'current_price_eur'} for r in ROWS[:20]]
        client = self.client({PAGE1: [page(rows)]})
        with self.assertRaises(PolicyError):
            client.collect()

    def test_20_pagination_outside_offers_is_refused(self):
        client = self.client({PAGE1: [page(ROWS[:2], 'https://ebag.bg/categories/418/products/json?page=2')]})
        with self.assertRaises(PolicyError) as caught:
            client.collect()
        self.assertIn('Unexpected pagination target', str(caught.exception))
        self.assertFalse(any('/categories/418/' in url for url in self.requested(client)))

    def test_20b_only_the_offers_endpoint_is_accepted(self):
        self.assertTrue(is_offers_page(PAGE1))
        self.assertTrue(is_offers_page('https://ebag.bg/categories/offers-products/json?mode=all_offers'))
        for url in ('https://ebag.bg/categories/418/products/json?page=6', 'http://ebag.bg/categories/offers-products/json?mode=all_offers',
                    'https://ebag.bg.evil.example/categories/offers-products/json?mode=all_offers',
                    'https://ebag.bg/categories/offers-products/json?mode=other', 'https://ebag.bg/categories/offers-products/json?mode=all_offers&page=0',
                    'https://ebag.bg/categories/offers-products/json?mode=all_offers&page=2&sortBy=x', 'https://ebag.bg/products/1/json'):
            self.assertFalse(is_offers_page(url), url)
        self.assertTrue(is_product_url('https://ebag.bg/mandarini-sort-primasol/641952'))
        for url in ('https://evil.example/x/1', 'https://ebag.bg/x', 'https://ebag.bg/../x/1', None):
            self.assertFalse(is_product_url(url), url)

    def test_incident_stale_page_from_earlier_days_is_never_requested(self):
        """The old crawler re-queued pages saved by earlier runs; one had stopped existing."""
        Path(self.dir + '/frontier-8c9202959adc.json').write_text(json.dumps(['https://ebag.bg/categories/418/products/json?page=6']))
        client = self.client({PAGE1: [page(ROWS[:5])]})
        client.collect()
        self.assertEqual(self.requested(client), ['https://ebag.bg/robots.txt', 'https://ebag.bg/terms', PAGE1])

    def test_page_budget_is_reported(self):
        client = self.client({PAGE1: [page(ROWS[:5], PAGE2, count=10)], PAGE2: [page(ROWS[5:10])]}, max_pages=1)
        client.collect()
        self.assertTrue(client.stats['listing_truncated'])
        self.assertIn('Лимитът', client.stats['warnings'][0])

    def test_low_coverage_is_a_warning(self):
        client = self.client({PAGE1: [page(ROWS[:5], count=100)]})
        client.collect()
        self.assertIn('5 от обявени 100', client.stats['warnings'][0])

    def test_request_log_has_no_secrets(self):
        client = self.client({PAGE1: [page(ROWS[:2])]})
        with self.assertLogs('ebag.http', 'INFO') as logs:
            client.collect()
        entries = [json.loads(line.split(':', 2)[2]) for line in logs.output]
        self.assertEqual({e['kind'] for e in entries}, {'policy', 'offers_page'})
        for entry in entries:
            self.assertTrue({'ts', 'method', 'url', 'status', 'redirects', 'attempt', 'timeout', 'op'} <= set(entry))
            self.assertNotRegex(json.dumps(entry).lower(), 'password|cookie|authorization|smtp')


class Scheduler(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = Repository(folder.name + '/db.sqlite3')
        self.addCleanup(self.repo.conn.close)
        self.at = datetime(2026, 11, 24, 8, 0, tzinfo=ZoneInfo('Europe/Sofia'))

    def test_404_fails_on_first_attempt_with_a_diagnosis(self):
        alerts = []
        error = FetchError('HTTP Error 404', dict(status=404, url=PAGE1, stage='оферти, страница 1', kind='offers_page', attempts=1), False)
        error.run_id = 42

        def run():
            raise error
        with patch('sys.stderr'):
            self.assertEqual(tick(self.repo, C, run, self.at, lambda kind, detail: alerts.append((kind, detail))), 'failed')
        self.assertEqual(get_slot(self.repo, '2026-11-24')['attempts'], 1)
        kind, detail = alerts[0]
        self.assertEqual(kind, 'crawl_failed')
        self.assertEqual((detail['status'], detail['url'], detail['run_id'], detail['transient']), (404, PAGE1, 42, False))
        self.assertIn('1 за деня', detail['attempts_text'])

    def test_transient_failure_is_still_retried(self):
        def run():
            raise FetchError('timeout', dict(status=None), True)
        with patch('sys.stderr'):
            self.assertEqual(tick(self.repo, C, run, self.at), 'retry')


class DropCheck(unittest.TestCase):
    def test_halving_against_last_successful_run_is_flagged(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Repository(folder + '/db.sqlite3')
            run = repo.start()
            repo.finish(run, 'success', 4000, 4000, 0, {'source': 'live', 'coverage': {'offers_count': 4289}})
            self.assertEqual(cli.drop_warning(repo, 3000), [])
            self.assertIn('необичайно голям', cli.drop_warning(repo, 2000)[0])
            repo.conn.close()

    def test_no_history_means_no_comparison(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Repository(folder + '/db.sqlite3')
            self.assertEqual(cli.drop_warning(repo, 10), [])
            repo.conn.close()
