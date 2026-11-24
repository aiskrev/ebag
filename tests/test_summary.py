"""Daily offers report and failure mail: email-safe HTML, plain text, no misleading output."""
import copy
import json
import os
import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from ebag import emails, summary
from ebag.core import prepare
from ebag.db import Repository
from ebag.notify import advice, deliver, operator_alert

C = json.loads(Path('config/preferences.yaml').read_text())
TODAY = date(2026, 11, 20)
SOURCE = 'https://ebag.bg/categories/offers-products/json?page=1&mode=all_offers'
STATS = {'products': 3, 'errors': 0, 'status': 'success', 'started_at': '2026-11-24T06:00:00+00:00', 'warnings': [],
         'coverage': {'offers_count': 3, 'unique_offers': 3}}
DEAL = dict(excluded=False, real_discount=30, classification='BUY', score=80, confidence='HIGH', quantity='BUY 1', watchlist=False, flags=[])
RECIPIENT = {'NOTIFICATION_EMAIL': 'owner@example.test'}


def item(name, price, old=None, shown=None, unit_price=2.0, excluded=False, source='offers', availability='in_stock', url='https://ebag.bg/produkt/1'):
    product = dict(name=name, normalized_name=name.lower(), price=price, regular_price=old, displayed_discount=shown, normalized_price=unit_price,
                   unit='kg', original_package_label='500 г', category='fruit', availability=availability, url=url, source=source,
                   source_url=SOURCE if source == 'offers' else None, external_id=name)
    return (1, product, dict(excluded=excluded, classification='IGNORE', watchlist=False, real_discount=None, confidence='LOW', flags=[]))


class Prices(unittest.TestCase):
    def test_14_discount_is_computed_from_valid_prices(self):
        self.assertEqual(emails.discount({'price': 1.5, 'regular_price': 2.0}), (25.0, 0.5))

    def test_store_figure_wins_when_present(self):
        self.assertEqual(emails.discount({'price': 1.5, 'regular_price': 2.0, 'displayed_discount': 24})[0], 24.0)

    def test_11_missing_old_price(self):
        self.assertEqual(emails.discount({'price': 1.5, 'regular_price': None}), (None, None))
        self.assertEqual(emails.money(None), 'Няма данни')

    def test_12_missing_new_price(self):
        self.assertEqual(emails.discount({'price': None, 'regular_price': 2.0}), (None, None))

    def test_13_invalid_prices_never_produce_a_discount(self):
        for new, old in (('abc', 2), (1.5, 'x'), (2, 0), (3, 2), (-1, 2), (float('nan'), 2), (1, float('inf'))):
            self.assertIsNone(emails.discount({'price': new, 'regular_price': old})[0], (new, old))
        self.assertEqual(emails.money('abc'), 'Няма данни')

    def test_prices_have_two_decimals_and_a_comma(self):
        self.assertEqual(emails.money(1.5), '1,50 €')
        self.assertEqual(emails.unit_price({'normalized_price': 7.369, 'unit': 'kg'}), '7,37 €/кг')


class OffersMail(unittest.TestCase):
    def build(self, items, stats=STATS, config=C):
        return summary.build(items, config, stats, TODAY)

    def test_16_table_has_the_columns_sorted_by_discount(self):
        subject, text, markup = self.build([item('Малка', 1.9, 2.0), item('Голяма', 1.0, 2.0), item('Без стара цена', 1.0)])
        self.assertTrue(subject.startswith('eBag оферти – дневен отчет'))
        for column in ('№', 'Продукт', 'Разфасовка', 'Стара цена', 'Офертна цена', 'Отстъпка', 'Цена за кг/л', 'Наличност', 'Връзка'):
            self.assertIn(f'>{column}</th>', markup)
        order = [markup.index(n) for n in ('Голяма', 'Малка', 'Без стара цена')]
        self.assertEqual(order, sorted(order))
        self.assertIn('Отвори продукта', markup)
        self.assertNotIn('<script', markup.lower())
        self.assertNotIn('<link', markup.lower())
        self.assertIn('https://ebag.bg/offers', markup)

    def test_long_urls_are_not_visible_text(self):
        url = 'https://ebag.bg/' + 'dulgo-ime-' * 20 + '/1'
        _, _, markup = self.build([item('Продукт', 1.0, 2.0, url=url)])
        visible = re.sub(r'<[^>]+>', ' ', markup)
        self.assertNotIn(url, visible)
        self.assertIn(f'href="{url}"', markup)

    def test_missing_values_say_no_data_never_none(self):
        _, text, markup = self.build([item('Продукт', 1.0, unit_price=None, availability=None)])
        self.assertIn('Няма данни', markup)
        self.assertNotIn('None', markup)
        self.assertNotIn('None', text)

    def test_15_names_are_escaped(self):
        _, _, markup = self.build([item('<img src=x onerror=alert(1)> & "кавички"', 1.0, 2.0)])
        self.assertNotIn('<img src=x', markup)
        self.assertIn('&lt;img src=x onerror=alert(1)&gt; &amp; &quot;кавички&quot;', markup)

    def test_17_plain_text_fallback(self):
        _, text, _ = self.build([item('Ябълки', 1.0, 2.0)])
        self.assertIn('1. Ябълки | 500 г | стара 2,00 € | офертна 1,00 € | отстъпка 50%', text)
        self.assertIn('https://ebag.bg/produkt/1', text)
        self.assertTrue(text.rstrip().endswith(emails.DISCLAIMER))

    def test_exact_disclaimer_and_header_facts(self):
        _, text, markup = self.build([item('Ябълки', 1.0, 2.0)])
        self.assertEqual(emails.DISCLAIMER, 'Това е автоматично генерирано служебно съобщение и не представлява препоръка за покупка.')
        self.assertIn(emails.DISCLAIMER, markup)
        for fact in ('Дата и час на обхода: 24.11.2026 08:00', 'Източник: https://ebag.bg/offers', 'Намерени оферти: 3',
                     'Успешно обработени: 3', 'Пропуснати артикули: 0', 'Общ статус на обхода: Успешен'):
            self.assertIn(fact, text)

    def test_18_no_offers_sends_a_diagnosis_not_an_empty_table(self):
        subject, text, markup = self.build([])
        self.assertIn('няма оферти', subject)
        self.assertNotIn('<th', markup)
        self.assertIn('Следващо действие', text)

    def test_blacklisted_offers_are_hidden_and_counted(self):
        _, text, markup = self.build([item('Бира', 1.0, 2.0, excluded=True), item('Ябълки', 1.0, 2.0)])
        self.assertNotIn('Бира', markup)
        self.assertIn('Скрити по blacklist: 1', text)

    def test_20_items_not_from_offers_block_the_report(self):
        with self.assertRaises(summary.ProvenanceError):
            self.build([item('Ябълки', 1.0, 2.0), item('От категория', 1.0, 2.0, source='category')])

    def test_partial_run_shows_its_warnings(self):
        stats = dict(STATS, status='partial', warnings=['Офертите са 2000, а при последния успешен обход бяха 4289.'])
        _, text, markup = self.build([item('Ябълки', 1.0, 2.0)], stats)
        self.assertIn('Общ статус на обхода: Частично успешен', text)
        self.assertIn('4289', markup)

    def test_rows_are_capped_to_stay_under_gmail_clipping(self):
        items = [item(f'Продукт с доста дълго име номер {n}', 1.0, 2.0 + n / 100) for n in range(300)]
        _, text, markup = self.build(items)
        self.assertEqual(markup.count('Отвори продукта'), 50)
        self.assertIn('Показани са първите 50 от 300', text)
        self.assertLess(len(markup.encode()), 100_000)

    def test_review_warning_near_expiry(self):
        config = copy.deepcopy(C)
        config['crawl'].update(policy_reviewed_at='2026-11-04', policy_review_days=30)
        self.assertIsNone(summary.review_warning(config, date(2026, 11, 26)))
        self.assertIn('04.12.2026', summary.review_warning(config, date(2026, 11, 27)))


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
        patcher = patch.dict(os.environ, RECIPIENT)
        patcher.start()
        self.addCleanup(patcher.stop)

    def send(self, provider, config=C):
        return summary.send(self.repo, [item('Ябълки', 1.0, 2.0)], config, provider, STATS)

    def test_once_per_local_day(self):
        provider = Provider()
        self.assertEqual(self.send(provider), 1)
        self.assertEqual(self.send(provider), 0)
        with patch.object(summary, 'local_today', return_value=date.today() + timedelta(days=1)):
            self.assertEqual(self.send(provider), 1)
        self.assertEqual(len(provider.messages), 2)

    def test_can_be_disabled(self):
        config = copy.deepcopy(C)
        config['daily_summary']['enabled'] = False
        provider = Provider()
        self.assertEqual(self.send(provider, config), 0)

    def test_failed_send_is_not_recorded_so_a_later_run_can_retry(self):
        class Broken(Provider):
            def send(self, *args):
                raise RuntimeError('down')
        with self.assertRaises(RuntimeError):
            self.send(Broken())
        self.assertEqual(self.send(Provider()), 1)

    def test_report_does_not_trigger_deal_cooldown(self):
        provider = Provider()
        self.send(provider)
        prod = prepare(dict(name='Яйца 10 бр', external_id='TEST', price=2, category='eggs', availability='in_stock', observed_at='2026-10-24T08:00:00+00:00'))
        pid, _ = self.repo.save(prod, self.repo.start())
        self.repo.conn.commit()
        self.assertEqual(deliver(self.repo, [(pid, prod, DEAL)], C, provider), 1)


class FailureMail(unittest.TestCase):
    DIAG = dict(status=404, url='https://ebag.bg/categories/offers-products/json?page=1&mode=all_offers', stage='оферти, страница 1', kind='offers_page', attempts=1,
                error='HTTP 404 Not Found', ts='2026-11-24T06:00:05+00:00', run_id=42, transient=False, attempts_text='1 за деня; последният направи 1 HTTP опита')

    def test_failure_mail_is_a_facts_table_with_a_concrete_step(self):
        provider = Provider()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, RECIPIENT):
            repo = Repository(folder + '/db.sqlite3')
            self.assertEqual(operator_alert(repo, C, provider, '2026-11-24', 'crawl_failed', self.DIAG), 1)
            repo.conn.close()
        _, subject, text, markup = provider.messages[0]
        self.assertEqual(subject, 'eBag агент: Дневният обход не успя')
        for label in ('Статус', 'Етап', 'HTTP статус', 'URL', 'Опити', 'Последна грешка', 'Време', 'Следващо действие'):
            self.assertIn(f'>{label}</td>', markup)
        self.assertIn('HTTP статус: 404', text)
        self.assertIn('Време: 24.11.2026 08:00', text)
        self.assertIn('Run ID за логовете: 42', text)
        self.assertIn('ebag run --send', text)
        self.assertIn('Повторен опит без проверка няма смисъл', text)
        self.assertNotIn('Traceback', text)
        self.assertNotEqual(advice('crawl_failed', self.DIAG).strip(), 'Проверете логовете.')

    def test_advice_differs_by_cause(self):
        self.assertIn('Повторният опит има смисъл', advice('crawl_failed', dict(status=503, transient=True)))
        self.assertIn('Retry-After: 120', advice('access_denied', dict(status=429, retry_after='120')))
        self.assertIn('не е временна', advice('crawl_failed', dict(status=None, transient=False, stage='parser')))
