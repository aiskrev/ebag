import copy,json,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from ebag.core import prepare,package,history,evaluate,blocked,watch
from ebag.db import Repository
from ebag.notify import deliver
from ebag.crawler import parse_jsonld,PolicyError,Client
C=json.loads(Path('config/preferences.yaml').read_text())
def product(**kw):
    r=dict(external_id='x',name='Пилешко филе 600 г',price='4.49',currency='EUR',category='fresh_meat',availability='in_stock',observed_at='2026-10-24T08:00:00+00:00');r.update(kw);return prepare(r)
class Tests(unittest.TestCase):
    def test_units_and_multipack(self):
        self.assertEqual(package('2 x 400 g'),(.8,'kg',2));self.assertEqual(package('6 x 1.5 liter'),(9,'liter',6));self.assertEqual(package('10 бр'),(10,'piece',1))
        self.assertAlmostEqual(product()['normalized_price'],7.483333,places=5)
    def test_unknown_and_variable_weight(self):
        self.assertIsNone(product(name='Unknown')['normalized_price'])
        self.assertIsNone(product(variable_weight=True)['normalized_price'])
        self.assertEqual(product(variable_weight=True,unit_price=8,unit='kg')['normalized_price'],8)
    def test_currency_and_bad_values(self):
        self.assertAlmostEqual(product(price='1.95583',currency='BGN')['price'],1)
        for v in ('NaN','Infinity','0','-1'):
            with self.assertRaises(ValueError):product(price=v)
    def test_blacklist(self):
        for x in ('Бира 500 мл','Вино 1 л','водка 1 л'):self.assertTrue(blocked(product(name=x),C))
        self.assertFalse(blocked(product(name='Ромашка чай 100 г'),C))
    def test_identity_package_change(self):
        self.assertNotEqual(product()['identity_hash'],product(name='Пилешко филе 450 г')['identity_hash'])
    def test_history_daily_dedup_current_excluded(self):
        rows=[dict(observed_at=f'2026-10-23T{h}:00:00+00:00',normalized_price=10,availability='in_stock') for h in ('10','11')]
        rows.append(dict(observed_at='2026-10-24T07:00:00+00:00',normalized_price=1,availability='in_stock'))
        h=history(rows,product()['observed_at'],C);self.assertEqual(h['observations'],1);self.assertEqual(h['median'],10);self.assertIsNone(h['median_30'])
    def test_synthetic_good_deal_and_fake_reference(self):
        p=product(price=4.2);t=datetime.fromisoformat(p['observed_at'])
        rows=[dict(observed_at=(t-timedelta(days=i)).isoformat(),normalized_price=10,availability='in_stock') for i in range(1,101)]
        h=history(rows,p['observed_at'],C);self.assertEqual(h['confidence'],'HIGH')
        d=evaluate(p,h,C);self.assertIn(d['classification'],('BUY','STRONG BUY'))
        p=product(price=5.8,regular_price=9);d=evaluate(p,h,C)
        self.assertIn('HISTORICAL DISCOUNT LOWER THAN DISPLAYED',d['flags']);self.assertEqual(d['classification'],'IGNORE')
    def test_cold_start_no_buy(self):
        p=product(regular_price=100);d=evaluate(p,history([],p['observed_at'],C),C)
        self.assertNotIn(d['classification'],('BUY','STRONG BUY'))
    def test_watch_bounds(self):
        c=copy.deepcopy(C);c['watchlist']=[dict(keywords='пилешко филе',max_price=3)]
        self.assertIsNone(watch(product(),c))
    def test_append_only_repository(self):
        with tempfile.TemporaryDirectory() as td:
            repo=Repository(td+'/db');p=product()
            a=repo.start();pid,_=repo.save(p,a);repo.conn.commit();repo.finish(a,'success',1,1)
            b=repo.start();repo.save(p,b);repo.conn.commit()
            self.assertEqual(len(repo.history(pid,'kg')),2);repo.conn.close()
    def test_empty_email_and_dedup(self):
        class Fake:
            def __init__(self):self.n=0
            def send(self,*args):self.n+=1
        with tempfile.TemporaryDirectory() as td:
            repo=Repository(td+'/db');p=product();run=repo.start();pid,_=repo.save(p,run);repo.conn.commit()
            d=dict(excluded=False,real_discount=30,classification='BUY',score=80,confidence='HIGH',quantity='BUY 1',watchlist=False,flags=[])
            f=Fake();self.assertEqual(deliver(repo,[],C,f),0)
            self.assertEqual(deliver(repo,[(pid,p,d)],C,f),1)
            self.assertEqual(deliver(repo,[(pid,p,d)],C,f),0);self.assertEqual(f.n,1);repo.conn.close()
    def test_jsonld_availability_unknown(self):
        data={'@type':'Product','name':'x','offers':{'price':'4.49','priceCurrency':'EUR'}}
        result=parse_jsonld([json.dumps(data)],'https://ebag.bg/offers');self.assertEqual(result[0]['availability'],'unknown')
    def test_client_rejects_external_host(self):
        with tempfile.TemporaryDirectory() as td:
            client=Client(C,td)
            with self.assertRaises(PolicyError):client.get('https://evil.test/',True)
if __name__=='__main__':unittest.main()

class MatchingTests(unittest.TestCase):
    def test_shrinkflation(self):
        from ebag.matching import compare,shrink
        old=product(name='Cheese 500 g',brand='Example',category='dairy',price=5)
        new=product(name='Cheese 450 g',brand='Example',category='dairy',price=5)
        kind,confidence=compare(old,new)
        self.assertEqual(kind,'SAME PRODUCT DIFFERENT PACKAGE')
        result=shrink(old,new,confidence)
        self.assertAlmostEqual(result['package_change_percent'],-10)
        self.assertAlmostEqual(result['normalized_price_increase_percent'],11.111111,places=5)
    def test_brand_mismatch(self):
        from ebag.matching import compare
        old=product(brand='One');new=product(brand='Two',name='Пилешко филе 450 г')
        self.assertEqual(compare(old,new)[0],'DIFFERENT PRODUCT')
    def test_official_unit_price_without_package(self):
        self.assertEqual(product(name='Variable fish',unit_price=8,unit='kg',variable_weight=True)['normalized_price'],8)
