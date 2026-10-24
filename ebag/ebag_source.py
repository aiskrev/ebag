"""Adapter for the anonymous JSON responses used by the public eBag storefront."""
from datetime import datetime,timezone
from decimal import Decimal
import re
from .core import package, normalized
UNITS={'кг':'kg','kg':'kg','л':'liter','l':'liter','бр':'piece','бр.':'piece'}
def classify(path):
    p=normalized(path or '')
    rules=[('alcohol',['алкохол','бира','вино','спиртни','уиски','ракия','водка','ликьор']),('household',['перилни','съдомиялна','почистващи','тоалетна хартия','кухненска хартия','домакински']),('minced_meat',['кайма','кюфтета','кебапчета','бургери']),('fish',['риба','морски','сьомга','херинга','хайвер']),('fresh_meat',['месо','свинско','телешко','пилешко','пуешко']),('eggs',['яйца']),('fruit',['плодове']),('vegetables',['зеленчуци']),('dairy',['млечни','мляко','айрян','сирене','кашкавал','масло']),('rice',['ориз']),('canned',['консерви','доматени продукти']),('legumes',['бобови']),('frozen',['замразени']),('bread',['хляб'])]
    # Resolve from deepest category outward. Shared roots such as
    # "Месо и риба" and "Млечни и яйца" must not override a specific leaf.
    for segment in reversed((path or '').split('/')):
        segment=normalized(segment)
        if segment in ('месо и риба','млечни и яйца','плодове и зеленчуци'):continue
        for label,terms in rules:
            if any(x in segment for x in terms):return label
    return 'other'

def date(s):
    return datetime.strptime(s,'%d/%m/%Y').date().isoformat() if s else None

def adapt(d,observed_at=None):
    if not isinstance(d,dict):raise ValueError('Product must be an object')
    for k in ('id','name','url_slug','current_price_eur','unit_type','unit_weight_text','prices_data_per_currency'):
        if k not in d:raise ValueError(f'eBag schema drift: missing {k}')
    prices=d['prices_data_per_currency']['EUR']; promo=bool(d.get('is_promo'))
    pprice=prices.get('promo_unit_delta') if promo else None
    current=pprice or prices.get('unit_delta')
    regular=prices.get('unit_delta') if promo else None
    if current is None:raise ValueError('Missing EUR display price')
    q,unit,count=package(d['unit_weight_text'])
    physical_package=d['unit_weight_text']
    if q is None:
        nq,nu,nc=package(d['name'])
        dose_multiplier=re.match(r'\s*(\d+)\s*[xх×]',d['unit_weight_text'])
        if nq and nu in ('kg','liter'):
            factor=int(dose_multiplier[1]) if dose_multiplier and nc==1 else 1
            q,unit,count=nq*factor,nu,nc*factor
            physical_package=f'{q:g} {unit}'
    variable=bool(d.get('unit_type_override')); unit_price=None
    cp=prices.get('count_type_price') or {}
    if variable:
        # EUR current_price is per kg; unit_delta is estimated package price.
        unit='kg';unit_price=d['current_price_eur']
    elif d['unit_type']==1:
        unit='kg';unit_price=d['current_price_eur']
    elif unit in ('kg','liter') and q:
        if UNITS.get(cp.get('base_unit'))==unit:
            unit_price=(cp.get('promo_price_per_base_unit') if promo else None) or cp.get('price_per_base_unit')
        elif UNITS.get(cp.get('bundle_base_unit'))==unit:
            unit_price=cp.get('price_per_bundle_item_base_unit')
        if unit_price is None:unit_price=str(Decimal(str(current))/Decimal(str(q)))
    elif unit=='piece':unit_price=str(Decimal(str(current))/Decimal(str(q))) if q else None
    starts=ends=None
    if d.get('promo_period'):
        parts=d['promo_period'].split(' - ')
        if len(parts)!=2:raise ValueError('Unexpected promotion period')
        starts,ends=map(date,parts)
    brand=d.get('brand');brand=(brand.get('name') or None) if isinstance(brand,dict) else brand
    cat=d.get('seo_category');path=(cat or '').split('/')
    b=prices.get('bundle_discount_data') or {}
    if not regular and b.get('original_price'):regular=b['original_price']
    return {'external_id':str(d['id']),'sku':d.get('product_code'),'name':d['name'],'name_en':d.get('name_en'),'brand':brand,'category':classify(cat),'subcategory':path[-1] if path else None,'category_path':cat,'url':f"https://ebag.bg/{d['url_slug']}/{d['id']}",'image_url':None,'price':current,'regular_price':regular,'currency':'EUR','package':physical_package,'original_package_label':d['unit_weight_text'],'variable_weight':variable,'unit':unit,'unit_price':unit_price,'availability':'in_stock' if d.get('is_available') is True else 'out_of_stock' if d.get('is_available') is False else 'unknown','observed_at':observed_at or datetime.now(timezone.utc).isoformat(),'promotion_start':starts,'promotion_end':ends,'expiration_date':date(d.get('expiry_date')),'displayed_discount':d.get('discount_percent') if d.get('discount_percent') is not None else b.get('percent'),'country_of_origin':d.get('country_of_origin'),'is_promo':promo,'shelf_stable':False,'storage_verified':False,'source_json':d,'parser_version':'ebag-json-0.2'}
