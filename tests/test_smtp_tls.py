"""Full pipeline against a real local STARTTLS server: no eBag access, no real mail.

Everything after the crawl is the production path: cli.run, scoring, deliver and
the SMTP provider speaking the real protocol with certificate verification.
"""
import base64
import email
import email.policy
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from ebag import cli
from ebag.core import prepare
from ebag.db import Repository
from ebag.ebag_source import adapt
from ebag.notify import SMTP, operator_alert

C = json.loads(Path('config/preferences.yaml').read_text())
ROWS = json.loads(Path('tests/fixtures/ebag-offers-20261024.json').read_text())['results']
OBSERVED = '2026-10-24T08:00:00+00:00'
FISH = 580633


class LocalSmtp(threading.Thread):
    """Minimal ESMTP server: refuses mail and AUTH until STARTTLS has happened."""

    def __init__(self, cert, key, port=0, implicit=False):
        super().__init__(daemon=True)
        self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls.load_cert_chain(cert, key)
        self.implicit = implicit
        self.listener = socket.create_server(('127.0.0.1', port))
        self.port = self.listener.getsockname()[1]
        self.messages = []
        self.logins = []
        self.commands_before_tls = []

    def stop(self):
        # close() alone leaves a thread blocked in accept holding the port.
        self.listener.shutdown(socket.SHUT_RDWR)
        self.listener.close()
        self.join(5)

    def run(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            self.active = conn
            try:
                self.serve()
            except (OSError, ssl.SSLError):
                pass
            finally:
                self.active.close()

    def serve(self):
        conn = self.active
        if self.implicit:
            conn = self.active = self.tls.wrap_socket(conn, server_side=True)
        secure = self.implicit
        reader = conn.makefile('rb')

        def say(text):
            conn.sendall(text.encode() + b'\r\n')
        say('220 local ESMTP')
        while True:
            line = reader.readline().decode('utf-8', 'replace').strip()
            verb = line.split(' ')[0].upper()
            if not secure:
                self.commands_before_tls.append(verb)
            if verb in ('EHLO', 'HELO'):
                say('250-local')
                say('250 AUTH PLAIN' if secure else '250 STARTTLS')
            elif verb == 'STARTTLS':
                say('220 go ahead')
                conn = self.active = self.tls.wrap_socket(conn, server_side=True)
                reader = conn.makefile('rb')
                secure = True
            elif not secure and verb != 'QUIT':
                say('530 STARTTLS required')
            elif verb == 'AUTH':
                self.logins.append(base64.b64decode(line.split(' ')[2]).split(b'\0')[1:])
                say('235 ok')
            elif verb in ('MAIL', 'RCPT'):
                say('250 ok')
            elif verb == 'DATA':
                say('354 go')
                body = b''
                while not body.endswith(b'\r\n.\r\n'):
                    body += reader.readline()
                self.messages.append(email.message_from_bytes(body[:-5], policy=email.policy.default))
                say('250 queued')
            elif verb == 'QUIT' or not line:
                say('221 bye')
                return
            else:
                say('502 unsupported')


@unittest.skipUnless(shutil.which('openssl'), 'openssl is needed to create a test certificate')
class Pipeline(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = folder.name
        self.cert, key = self.dir + '/cert.pem', self.dir + '/key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=localhost',
                        '-addext', 'subjectAltName=DNS:localhost', '-keyout', key, '-out', self.cert], check=True, capture_output=True)
        self.server = LocalSmtp(self.cert, key)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.env = {'SMTP_HOST': 'localhost', 'SMTP_PORT': str(self.server.port), 'SMTP_USERNAME': 'agent', 'SMTP_PASSWORD': 'test-only',
                    'SMTP_FROM': 'agent@example.test', 'SMTP_USE_TLS': 'true', 'NOTIFICATION_EMAIL': 'owner@example.test', 'SSL_CERT_FILE': self.cert}
        patcher = patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed_history(self, db, row):
        """100 earlier days at twice today's unit price, so today is a proven deal."""
        repo = Repository(db)
        start = datetime.fromisoformat(OBSERVED)
        for days in range(100, 0, -1):
            p = prepare(adapt(row, (start - timedelta(days=days)).isoformat()))
            p['normalized_price'] *= 2
            # Earlier days are full-price days; otherwise this reads as a permanent promotion.
            p['raw'] = dict(p['raw'], is_promo=False)
            repo.save(p, repo.start())
        repo.conn.commit()
        repo.conn.close()

    def crawl(self, db):
        row = next(r for r in ROWS if r['id'] == FISH)

        class Offline:
            stats = {'listing_pages': 1, 'listing_truncated': False, 'tracked_pending': 0, 'missing_products': [], 'parse_errors': [], 'parse_error_count': 0, 'release_drift': None}

            def __init__(self, c):
                pass

            def collect(self, tracked):
                return [adapt(row, OBSERVED)]

            def save_state(self):
                pass
        with patch.object(cli, 'Client', Offline), patch('builtins.print'):
            return cli.run(C, db, send=True, output=self.dir + '/report.json')

    def test_deal_reaches_the_server_once_over_verified_tls(self):
        db = self.dir + '/db.sqlite3'
        self.seed_history(db, next(r for r in ROWS if r['id'] == FISH))
        result = self.crawl(db)
        self.assertEqual((result['status'], result['emails'], result['email_error']), ('success', 1, None))
        self.assertEqual(self.server.logins, [[b'agent', b'test-only']])
        self.assertNotIn('MAIL', self.server.commands_before_tls)
        self.assertNotIn('AUTH', self.server.commands_before_tls)
        message = self.server.messages[0]
        self.assertEqual(message['To'], 'owner@example.test')
        self.assertEqual(message['Subject'], 'eBag: добри цени според собствената история')
        self.assertEqual(sorted(part.get_content_type() for part in message.iter_parts()), ['text/html', 'text/plain'])
        text = message.get_body(('plain',)).get_content()
        self.assertIn('EUR/kg', text)
        self.assertIn('https://ebag.bg/', text)
        repo = Repository(db)
        self.addCleanup(repo.conn.close)
        self.assertEqual(repo.conn.execute('SELECT status FROM notifications').fetchone()[0], 'sent')
        self.assertEqual(self.crawl(db)['emails'], 0, 'identical offer must not be sent twice')
        self.assertEqual(len(self.server.messages), 1)

    def test_untrusted_certificate_is_refused_and_keeps_the_crawl(self):
        db = self.dir + '/db.sqlite3'
        self.seed_history(db, next(r for r in ROWS if r['id'] == FISH))
        del os.environ['SSL_CERT_FILE']
        result = self.crawl(db)
        self.assertEqual(result['status'], 'success')
        self.assertIn('CERTIFICATE_VERIFY_FAILED', result['email_error'])
        self.assertEqual(self.server.messages, [])
        self.assertEqual(self.server.logins, [], 'credentials must never reach an unverified server')
        repo = Repository(db)
        self.addCleanup(repo.conn.close)
        self.assertEqual(repo.conn.execute('SELECT status FROM notifications').fetchone()[0], 'failed')
        # Nothing left the agent, so the offer goes out once mail works again.
        os.environ['SSL_CERT_FILE'] = self.cert
        self.assertEqual(self.crawl(db)['emails'], 1)
        self.assertEqual(len(self.server.messages), 1)

    def test_unreachable_server_is_not_marked_uncertain(self):
        db = self.dir + '/db.sqlite3'
        self.seed_history(db, next(r for r in ROWS if r['id'] == FISH))
        with socket.create_server(('127.0.0.1', 0)) as free:
            os.environ['SMTP_PORT'] = str(free.getsockname()[1])
        self.assertIn('SMTP connect failed', self.crawl(db)['email_error'])
        repo = Repository(db)
        self.addCleanup(repo.conn.close)
        self.assertEqual(repo.conn.execute('SELECT status FROM notifications').fetchone()[0], 'failed')

    def test_operator_alert_uses_the_same_transport(self):
        repo = Repository(self.dir + '/db.sqlite3')
        self.addCleanup(repo.conn.close)
        self.assertEqual(operator_alert(repo, C, SMTP(), '2026-11-04', 'review_required', 'Storefront release changed'), 1)
        self.assertIn('нужен е преглед', self.server.messages[0]['Subject'])
