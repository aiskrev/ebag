import copy,json,os,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch
from ebag.core import history,evaluate,prepare
from ebag.db import Repository
from ebag.notify import deliver
from ebag.crawler import Client,PolicyError
from ebag.report import render
C=json.loads(Path('config/preferences.yaml').read_text())
def p():return prepare(dict(name='Яйца 10 бр',external_id='TEST',price=2,category='eggs',availability='in_stock',observed_at='2026-10-24T08:00:00+00:00'))
class Operational(unittest.TestCase):
    def test_continuous_promotion(self):
        prod=p();prod['is_promo']=True;t=datetime.fromisoformat(prod['observed_at']);rows=[dict(observed_at=(t-timedelta(days=i)).isoformat(),normalized_price=1,availability='in_stock',is_promo=True) for i in range(1,101)]
        d=evaluate(prod,history(rows,prod['observed_at'],C),C)
        self.assertIn('POSSIBLE CONTINUOUS PROMOTION',d['flags']);self.assertNotIn(d['classification'],('BUY','STRONG BUY'))
    def test_expired_product_never_buy(self):
        prod=p();prod['expiration_date']='2026-10-20';t=datetime.fromisoformat(prod['observed_at']);rows=[dict(observed_at=(t-timedelta(days=i)).isoformat(),normalized_price=1,availability='in_stock') for i in range(1,101)]
        d=evaluate(prod,history(rows,prod['observed_at'],C),C);self.assertEqual(d['classification'],'IGNORE')
    def test_smtp_ambiguous_not_retried(self):
        class Broken:
            n=0
            def send(self,*args):self.n+=1;raise RuntimeError('ambiguous transport result')
        with tempfile.TemporaryDirectory() as td:
            repo=Repository(td+'/db');prod=p();run=repo.start();pid,_=repo.save(prod,run);repo.conn.commit()
            d=dict(excluded=False,real_discount=30,classification='BUY',score=80,confidence='HIGH',quantity='BUY 1',watchlist=False,flags=[])
            provider=Broken()
            with patch.dict(os.environ,{'NOTIFICATION_EMAIL':'aiskrev@abv.bg'}):
                with self.assertRaises(RuntimeError):deliver(repo,[(pid,prod,d)],C,provider)
                self.assertEqual(deliver(repo,[(pid,prod,d)],C,provider),0)
            self.assertEqual(provider.n,1);self.assertEqual(repo.conn.execute('SELECT status FROM notifications').fetchone()[0],'uncertain');repo.conn.close()
    def test_robots_deny_stops_request(self):
        import urllib.robotparser
        with tempfile.TemporaryDirectory() as td:
            client=Client(C,td);client.robot=urllib.robotparser.RobotFileParser();client.robot.parse(['User-agent: *','Disallow: /categories'])
            with self.assertRaises(PolicyError):client.get('https://ebag.bg/categories/offers-products/json?mode=all_offers')
    def test_render_escapes_names(self):
        prod=p();prod['name']='<script>alert(1)</script>';d=evaluate(prod,history([],prod['observed_at'],C),C)
        with tempfile.TemporaryDirectory() as td:
            render([(1,prod,d)],td+'/report.html');text=Path(td+'/report.html').read_text();self.assertNotIn('<script>alert(1)</script>',text);self.assertIn('&lt;script&gt;',text)


class SettingsTests(unittest.TestCase):
    def test_invalid_config_limits(self):
        from ebag.settings import load
        c=copy.deepcopy(C);c['crawl']['max_pages']=10000
        with tempfile.TemporaryDirectory() as td:
            f=Path(td+'/bad.json');f.write_text(json.dumps(c))
            with self.assertRaises(ValueError):load(f)
    def test_yaml_configuration(self):
        from ebag.settings import load
        import yaml
        with tempfile.TemporaryDirectory() as td:
            f=Path(td+'/config.yaml');f.write_text(yaml.safe_dump(C,allow_unicode=True));self.assertEqual(load(f)['timezone'],'Europe/Sofia')
