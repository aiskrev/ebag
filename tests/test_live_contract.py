"""Contract tests against captured anonymous eBag JSON (not simulated prices)."""
import copy,json,unittest
from pathlib import Path
from ebag.ebag_source import adapt,classify
from ebag.core import prepare

class LiveContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.rows=json.loads(Path('tests/fixtures/ebag-offers-20261024.json').read_text())['results']
    def get(self,pid):return prepare(adapt(next(r for r in self.rows if r['id']==pid)))
    def test_all_20_products(self):
        self.assertEqual(len(self.rows),20)
        for row in self.rows:
            p=prepare(adapt(row));self.assertGreater(p['price'],0);self.assertGreater(p['normalized_price'],0)
    def test_variable_fish(self):
        p=self.get(580633);self.assertEqual(p['price'],4.03);self.assertEqual(p['regular_price'],5.03);self.assertEqual(p['normalized_price'],7.19);self.assertEqual(p['unit'],'kg');self.assertTrue(p['variable_weight'])
        self.assertEqual(p['expiration_date'],'2026-10-28')
    def test_multipack(self):
        p=self.get(642938);self.assertEqual(p['quantity'],.2);self.assertEqual(p['normalized_price'],38.9);self.assertEqual(p['unit'],'kg')
        p=self.get(644932);self.assertEqual(p['quantity'],2);self.assertEqual(p['normalized_price'],1.29);self.assertEqual(p['unit'],'liter')
    def test_avocado_piece_not_name_weight(self):
        p=self.get(609792);self.assertEqual(p['unit'],'piece');self.assertEqual(p['quantity'],1);self.assertEqual(p['normalized_price'],1.89)
    def test_count_grams(self):
        p=self.get(635837);self.assertAlmostEqual(p['quantity'],.6);self.assertEqual(p['normalized_price'],7.42)
    def test_tomato_and_mandarines(self):
        self.assertEqual(self.get(18798)['normalized_price'],9.58);self.assertEqual(self.get(641952)['normalized_price'],1.49)
    def test_schema_drift(self):
        d=copy.deepcopy(self.rows[0]);del d['current_price_eur']
        with self.assertRaises(ValueError):adapt(d)
    def test_categories(self):
        self.assertEqual(classify('Плодове и зеленчуци/Зеленчуци/Домати'),'vegetables')
        self.assertEqual(classify('Напитки/Алкохол/Бира'),'alcohol')
        self.assertEqual(classify('Месо и риба/Месо/Пилешко месо'),'fresh_meat')
        self.assertEqual(classify('Млечни и яйца/Кашкавал'),'dairy')
        self.assertEqual(classify('Млечни и яйца/Яйца'),'eggs')


class DosePhysicalQuantity(unittest.TestCase):
    def test_multipack_doses_use_explicit_volume(self):
        d=json.loads(Path('tests/fixtures/ebag-offers-20261024.json').read_text())['results'][19]
        d['name']='Гел за пране 2.7 л';d['unit_weight_text']='2 x 60 пране'
        d['prices_data_per_currency']['EUR']['count_type_price']=None
        p=prepare(adapt(d));self.assertAlmostEqual(p['quantity'],5.4);self.assertEqual(p['unit'],'liter')
    def test_unknown_dose_weight_not_invented(self):
        d=json.loads(Path('tests/fixtures/ebag-offers-20261024.json').read_text())['results'][19]
        d['name']='Гел за пране';d['unit_weight_text']='40 пране'
        p=prepare(adapt(d));self.assertIsNone(p['normalized_price'])


class ApproximateWeightIdentity(unittest.TestCase):
    def test_weight_estimate_does_not_split_identity(self):
        d=json.loads(Path('tests/fixtures/ebag-fish-20261024.json').read_text())
        a=prepare(adapt(d));d['unit_weight_text']='~ 570 г / бр';d['unit_weight']='0.5700'
        b=prepare(adapt(d));self.assertEqual(a['identity_hash'],b['identity_hash'])
    def test_approximate_weight_not_shrinkflation(self):
        from ebag.matching import shrink
        a=dict(quantity=.57,unit='kg',price=5,normalized_price=10,variable_weight=True)
        b=dict(quantity=.56,unit='kg',price=5,normalized_price=11,variable_weight=True)
        self.assertIsNone(shrink(a,b,1))
