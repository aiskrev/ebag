"""Port 465 path, which providers such as abv.bg use: TLS from the first byte."""
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ebag import cli
from ebag.db import Repository
from ebag.notify import SMTP, NotSent
from test_smtp_tls import LocalSmtp

C = json.loads(Path('config/preferences.yaml').read_text())


@unittest.skipUnless(shutil.which('openssl'), 'openssl is needed to create a test certificate')
class ImplicitTls(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = folder.name
        self.cert, key = self.dir + '/cert.pem', self.dir + '/key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=localhost',
                        '-addext', 'subjectAltName=DNS:localhost', '-keyout', key, '-out', self.cert], check=True, capture_output=True)
        try:
            self.server = LocalSmtp(self.cert, key, port=465, implicit=True)
        except OSError as exc:
            self.skipTest(f'port 465 is not available here: {exc}')
        self.server.start()
        self.addCleanup(self.server.stop)
        env = {'SMTP_HOST': 'localhost', 'SMTP_PORT': '465', 'SMTP_USERNAME': 'agent@example.test', 'SMTP_PASSWORD': 'test-only',
               'SMTP_FROM': 'agent@example.test', 'SMTP_USE_TLS': 'true', 'NOTIFICATION_EMAIL': 'owner@example.test', 'SSL_CERT_FILE': self.cert}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_trusted_server_receives_mail(self):
        SMTP().send('owner@example.test', 'Тема', 'текст', '<p>текст</p>')
        self.assertEqual(self.server.logins, [[b'agent@example.test', b'test-only']])
        self.assertEqual(self.server.messages[0]['Subject'], 'Тема')

    def test_password_never_reaches_an_unverified_server(self):
        del os.environ['SSL_CERT_FILE']
        with self.assertRaises(NotSent) as caught:
            SMTP().send('owner@example.test', 'Тема', 'текст', '<p>текст</p>')
        self.assertIn('CERTIFICATE_VERIFY_FAILED', str(caught.exception))
        self.assertEqual(self.server.logins, [])
        self.assertEqual(self.server.messages, [])


class ContextIsVerifying(unittest.TestCase):
    """Runs everywhere, including where port 465 cannot be bound."""

    def test_port_465_passes_a_verifying_context(self):
        env = {'SMTP_HOST': 'smtp.example.test', 'SMTP_PORT': '465', 'SMTP_FROM': 'a@example.test', 'SMTP_USE_TLS': 'true', 'SMTP_USERNAME': ''}
        with patch.dict(os.environ, env), patch('ebag.notify.smtplib.SMTP_SSL') as client:
            SMTP().send('b@example.test', 's', 't', '<p>t</p>')
        context = client.call_args.kwargs['context']
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)


class TestMessage(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = folder.name + '/db.sqlite3'
        self.report = folder.name + '/report.json'

    def finish(self, source):
        repo = Repository(self.db)
        run = repo.start()
        repo.finish(run, 'success', 850, 850, 0, {'source': source})
        repo.conn.close()

    def test_without_live_crawl_says_so(self):
        self.finish('import')
        self.assertIn('Все още няма', cli.test_lines(self.db, self.report, C)[-1])

    def test_lists_cheapest_watchlist_item_and_is_marked_as_test(self):
        self.finish('live')

        def row(name, price, unit_price):
            product = dict(name=name, normalized_name=name.lower(), price=price, normalized_price=unit_price, unit='kg', url='https://ebag.bg/x/1')
            return {'product': product, 'assessment': {'excluded': False}}
        Path(self.report).write_text(json.dumps([row('Ябълки скъпи', 5, 5), row('Ябълки евтини', 2, 2), row('Сол', 1, 1)]))
        lines = cli.test_lines(self.db, self.report, C)
        text = '\n'.join(lines)
        self.assertIn('не е препоръка', lines[0])
        self.assertIn('850 продукта', text)
        self.assertIn('Ябълки евтини', text)
        self.assertNotIn('Ябълки скъпи', text)
        self.assertNotIn('Сол', text)
