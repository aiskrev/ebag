"""The daily digest reports observations and must not disturb deal alerts."""
import copy
import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from ebag import summary
from ebag.core import prepare
from ebag.db import Repository
from ebag.notify import deliver

C = json.loads(Path('config/preferences.yaml').read_text())
TODAY = date(2026, 11, 20)
STATS = {'products': 987, 'errors': 0, 'coverage': {'listing_truncated': False}}
DEAL = dict(excluded=False, real_discount=30, classification='BUY', score=80, confidence='HIGH', quantity='BUY 1', watchlist=False, flags=[])


def item(name, price, unit_price, percent=None, category='fruit', excluded=False, classification='IGNORE', availability='in_stock'):
    product = dict(name=name, normalized_name=name.lower(), price=price, normalized_price=unit_price, unit='kg', category=category, availability=availability, url='https://ebag.bg/x/1')
    assessment = dict(excluded=excluded, classification=classification, watchlist=False, real_discount=None, confidence='LOW', flags=[], metrics={'price_change_percent': percent})
    return (1, product, assessment)


class Text(unittest.TestCase):
    def text(self, items, days=5, stats=STATS, config=C, today=TODAY):
        return '\n'.join(summary.lines(items, config, stats, days, today))

    def test_never_reads_as_a_recommendation(self):
        text = self.text([item('Ябълки', 2, 2, -20)])
        self.assertIn('не препоръка за покупка', text)
        self.assertIn('Няма доказани оферти', text)
        self.assertNotIn('BUY', text)

    def test_counts_only_real_deals(self):
        deal = item('Ябълки', 2, 2)
        deal[2].update(DEAL)
        self.assertIn('Оферти, които отговарят на условията: 1', self.text([deal, item('Круши', 3, 3)], days=40))

    def test_mature_history_without_deals_says_so(self):
        self.assertIn('реално ниска спрямо историята', self.text([item('Ябълки', 2, 2)], days=40))

    def test_drops_are_sorted_filtered_and_limited(self):
        items = [item(f'Продукт {i}', 2, 2, -10 - i) for i in range(8)]
        items += [item('Малък спад', 2, 2, -4), item('Поскъпване', 2, 2, 30), item('Друга категория', 2, 2, -90, category='other'),
                  item('Изключен', 2, 2, -90, excluded=True), item('Изчерпан', 2, 2, -90, availability='out_of_stock')]
        drops = summary.biggest_drops(items, C, 5)
        self.assertEqual([p['name'] for _, p in drops], [f'Продукт {i}' for i in range(7, 2, -1)])

    def test_first_day_has_nothing_to_compare(self):
        self.assertIn('от следващия обход', self.text([item('Ябълки', 2, 2)], days=1))

    def test_watchlist_uses_cheapest_and_hides_excluded(self):
        text = self.text([item('Ябълки скъпи', 5, 5), item('Ябълки евтини', 2, 2, -12.5), item('Ябълки скрити', 1, 1, excluded=True)])
        self.assertIn('Ябълки евтини', text)
        self.assertIn('-12.5% спрямо предишното', text)
        self.assertNotIn('скъпи', text)
        self.assertNotIn('скрити', text)

    def test_units_are_never_compared_with_each_other(self):
        """Found on real data: 0.47 EUR/piece beat every price per kg."""
        per_piece = item('Ябълки в тарелка', 1.89, 0.47)
        per_piece[1]['unit'] = 'piece'
        text = self.text([per_piece, item('Ябълки Гала', 2.5, 2.5), item('Ябълки Златна', 1.9, 1.9)])
        self.assertIn('Ябълки Златна', text)
        self.assertNotIn('тарелка', text)

    def test_one_day_of_history_is_singular(self):
        self.assertIn('история: 1 ден.', self.text([item('Ябълки', 2, 2)], days=1))
        self.assertIn('история: 5 дни.', self.text([item('Ябълки', 2, 2)], days=5))

    def test_parser_errors_are_reported(self):
        self.assertIn('2 грешки', self.text([item('Ябълки', 2, 2)], stats=dict(STATS, errors=2)))

    def test_review_expiry_warning_only_near_the_end(self):
        config = copy.deepcopy(C)
        config['crawl'].update(policy_reviewed_at='2026-11-04', policy_review_days=30)
        self.assertNotIn('прегледът на условията', self.text([item('Ябълки', 2, 2)], config=config, today=date(2026, 11, 26)))
        self.assertIn('важи до 04.12.2026', self.text([item('Ябълки', 2, 2)], config=config, today=date(2026, 11, 27)))
        self.assertIn('важи до 04.12.2026', self.text([item('Ябълки', 2, 2)], config=config, today=date(2026, 12, 4)))


class Provider:
    def __init__(self):
        self.messages = []

    def send(self, recipient, subject, text, markup):
        self.messages.append((recipient, subject, text, markup))


class Sending(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = Repository(folder.name + '/db.sqlite3')
        self.addCleanup(self.repo.conn.close)
        patcher = patch.dict(os.environ, {'NOTIFICATION_EMAIL': 'aiskrev@abv.bg'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def send(self, provider, config=C):
        return summary.send(self.repo, [item('<b>Ябълки</b>', 2, 2)], config, provider, STATS)

    def test_once_per_local_day(self):
        provider = Provider()
        self.assertEqual(self.send(provider), 1)
        self.assertEqual(self.send(provider), 0)
        with patch.object(summary, 'local_today', return_value=date.today() + timedelta(days=1)):
            self.assertEqual(self.send(provider), 1)
        self.assertEqual(len(provider.messages), 2)
        self.assertIn('дневно резюме', provider.messages[0][1])

    def test_names_are_escaped_in_html(self):
        provider = Provider()
        self.send(provider)
        self.assertNotIn('<b>Ябълки', provider.messages[0][3])
        self.assertIn('&lt;b&gt;', provider.messages[0][3])

    def test_can_be_disabled(self):
        config = copy.deepcopy(C)
        config['daily_summary']['enabled'] = False
        provider = Provider()
        self.assertEqual(self.send(provider, config), 0)
        self.assertEqual(provider.messages, [])

    def test_failed_send_is_not_recorded_so_a_later_run_can_retry(self):
        class Broken(Provider):
            def send(self, *args):
                raise RuntimeError('down')
        with self.assertRaises(RuntimeError):
            self.send(Broken())
        provider = Provider()
        self.assertEqual(self.send(provider), 1)

    def test_summary_does_not_trigger_deal_cooldown(self):
        provider = Provider()
        self.send(provider)
        prod = prepare(dict(name='Яйца 10 бр', external_id='TEST', price=2, category='eggs', availability='in_stock', observed_at='2026-10-24T08:00:00+00:00'))
        pid, _ = self.repo.save(prod, self.repo.start())
        self.repo.conn.commit()
        self.assertEqual(deliver(self.repo, [(pid, prod, DEAL)], C, provider), 1)


class Configuration(unittest.TestCase):
    def check(self, value):
        from ebag.settings import load
        config = copy.deepcopy(C)
        config['daily_summary'] = value
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder + '/c.json')
            path.write_text(json.dumps(config))
            return load(path)

    def test_limits(self):
        self.assertTrue(self.check({'enabled': False, 'max_drops': 3}))
        for bad in ({'max_drops': 0}, {'max_drops': 21}, {'enabled': 'yes'}, 'yes'):
            with self.assertRaises(ValueError):
                self.check(bad)
