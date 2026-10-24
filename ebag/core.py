import hashlib, json, math, re, statistics, unicodedata
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
RATE = Decimal('1.95583')
def now(): return datetime.now(timezone.utc).isoformat()
def normalized(s):
    return ' '.join(re.sub(r'[^\w\s]', ' ', unicodedata.normalize('NFKC', s).casefold()).split())
def money(v, currency='EUR'):
    if v is None: return None
    try: d = Decimal(str(v).replace(',', '.').strip())
    except InvalidOperation: raise ValueError('Invalid price')
    if not d.is_finite() or d <= 0: raise ValueError('Price must be positive and finite')
    if currency == 'BGN': d /= RATE
    elif currency != 'EUR': raise ValueError('Unsupported currency')
    return float(d)
def package(text):
    s=text.casefold().replace(',', '.')
    m=re.search(r'(?:(\d+)\s*[xх×]\s*)?(\d+(?:\.\d+)?)\s*(kg|кг|g|гр|г|ml|мл|l|л|liter|литра|pcs|бр)\b',s)
    if not m: return None, None, None
    count=int(m[1] or 1); q=float(m[2]); u=m[3]
    if q<=0 or count<=0: return None,None,None
    if u in ('g','гр','г'): q/=1000; u='kg'
    elif u in ('ml','мл'): q/=1000; u='liter'
    elif u in ('kg','кг'): u='kg'
    elif u in ('pcs','бр'): u='piece'
    else: u='liter'
    return q*count,u,count
def prepare(raw):
    p=dict(raw); p['normalized_name']=normalized(p['name'])
    p['price']=money(p['price'],p.get('currency','EUR'))
    p['regular_price']=money(p.get('regular_price'),p.get('currency','EUR'))
    q,u,c=package(p.get('package') or p['name'])
    p.update(quantity=q, unit=u, package_count=c)
    # Official unit prices take precedence for variable/approximate weight.
    if p.get('unit_price') is not None:
        if raw.get('unit') not in ('kg','liter','piece'): raise ValueError('Unknown official unit')
        p['normalized_price']=money(p['unit_price'],p.get('currency','EUR')); p['unit']=raw['unit']
    elif q and not p.get('variable_weight'):
        p['normalized_price']=p['price']/q
    else: p['normalized_price']=None
    p['identity_hash']=hashlib.sha256(json.dumps([p.get('external_id') or p.get('url'),None if p.get('variable_weight') else q,p['unit'],c],ensure_ascii=False).encode()).hexdigest()
    p['raw']=raw
    return p
def blocked(p,c):
    text=normalized(' '.join(str(p.get(k) or '') for k in ('name','category','subcategory')))
    words=set(text.split())
    return (p.get('category') in c['excluded_categories'] or
            str(p.get('external_id')) in [str(x) for x in c['excluded_products']] or
            any(set(normalized(k).split()) <= words for k in c['excluded_keywords']))
def watch(p,c):
    for w in c['watchlist']:
        if not set(normalized(w['keywords']).split()) <= set(normalized(p['normalized_name']+' '+str(p.get('name_en') or '')).split()): continue
        if normalized(p.get('brand') or '') in [normalized(x) for x in w.get('excluded_brands',[])]: continue
        if w.get('brand') and normalized(w['brand'])!=normalized(p.get('brand') or ''): continue
        q=p.get('quantity')
        if w.get('min_quantity') is not None and (q is None or q<w['min_quantity']): continue
        if w.get('max_quantity') is not None and (q is None or q>w['max_quantity']): continue
        if w.get('max_price') is not None and p['price']>w['max_price']: continue
        if w.get('max_unit_price') is not None and (p.get('normalized_price') is None or p['normalized_price']>w['max_unit_price']): continue
        if w.get('unit') and w['unit']!=p.get('unit'): continue
        return w
    return None
def history(rows,observed_at,c):
    t=datetime.fromisoformat(observed_at); daily={};meta={}
    # Exclude current/future dates; at most one observation per local day.
    from zoneinfo import ZoneInfo
    zone=ZoneInfo(c['timezone']); today=t.astimezone(zone).date()
    for r in sorted(rows,key=lambda r:r['observed_at']):
        dt=datetime.fromisoformat(r['observed_at']); day=dt.astimezone(zone).date()
        if day<today and r.get('normalized_price') is not None and r.get('availability')=='in_stock':
            daily[day]=r['normalized_price'];meta[day]=r
    vals=list(daily.values()); span=(today-min(daily)).days if daily else 0
    h={'observations':len(vals),'days_tracked':span,'confidence':'LOW','median':None,'minimum':None,'maximum':None,'average':None,'stddev':None,'previous':None}
    if vals:
        h.update(median=statistics.median(vals),minimum=min(vals),maximum=max(vals),average=statistics.mean(vals),stddev=statistics.pstdev(vals),previous=vals[-1])
    hc=c['history']
    for label in ('medium','high'):
        if len(vals)>=hc[label+'_observations'] and span>=hc[label+'_days']: h['confidence']=label.upper()
    for days in (7,30,90,180,365):
        window=[v for d,v in daily.items() if 0<(today-d).days<=days]
        ok=span>=days and len(window)>=math.ceil(days*hc['window_coverage'])
        h[f'median_{days}']=statistics.median(window) if ok else None
        h[f'min_{days}']=min(window) if ok else None
        h[f'values_{days}']=window if ok else []
    for days in (30,90):
        window=[r for d,r in meta.items() if 0<(today-d).days<=days]
        h[f'promo_ratio_{days}']=sum(bool(r.get('is_promo')) for r in window)/len(window) if h.get(f'median_{days}') and window else None
    # Find a brief non-promotion spike immediately before the latest promotion.
    # Require adjacent days and an earlier normal-price observation.
    ordered=sorted(meta.items());j=len(ordered)-1
    while j>=0 and meta[ordered[j][0]].get('is_promo'):j-=1
    spike=[]
    while j>=0 and not meta[ordered[j][0]].get('is_promo'):
        spike.append(ordered[j]);j-=1
    adjacent=all((spike[i][0]-spike[i+1][0]).days==1 for i in range(len(spike)-1))
    h['brief_reference_price_spike']=bool(h.get('median_30') and 1<=len(spike)<=c['deal_thresholds'].get('short_reference_max_days',3) and adjacent and j>=0 and min(v for _,v in spike)>h['median_30']*(1+c['deal_thresholds'].get('reference_inflation_percent',20)/100) and ordered[j][1]<=h['median_30']*1.05)
    h['daily_price_change_percent']=((vals[-1]-vals[-2])/vals[-2]*100) if len(vals)>1 and list(daily)[-1]-list(daily)[-2]==timedelta(days=1) else None
    return h
def evaluate(p,h,c):
    th=c['deal_thresholds']; v=p.get('normalized_price'); flags=[]; factors={}; w=watch(p,c)
    weights=c.get('score_weights',{})
    baseline=h.get('median_90') or h.get('median_30') or h['median']
    real=(baseline-v)/baseline*100 if baseline and v else None
    display=(p['regular_price']-p['price'])/p['regular_price']*100 if p.get('regular_price') else None
    if h['confidence']=='LOW': flags.append('INSUFFICIENT HISTORY')
    if h['confidence']!='LOW' and real is not None:
        if display is not None and display-real>=th.get('display_gap_percent',15): flags.append('HISTORICAL DISCOUNT LOWER THAN DISPLAYED')
        if p.get('regular_price') and v and (p['regular_price']/p['price']*v)>baseline*(1+th.get('reference_inflation_percent',20)/100): flags.append('POSSIBLE INFLATED REFERENCE PRICE')
        values=h.get('values_30',[])
        if values and sum(x<=v for x in values)/len(values)>=th.get('frequent_low_price_ratio',.7): flags.append('PRICE FREQUENTLY AT OR BELOW CURRENT')
    if p.get('is_promo') and (h.get('promo_ratio_30') or 0)>=th.get('continuous_promo_ratio',.8):flags.append('POSSIBLE CONTINUOUS PROMOTION')
    if p.get('is_promo') and h.get('brief_reference_price_spike'):flags.append('POSSIBLE BRIEF REFERENCE PRICE INCREASE')
    factors['historical_discount']=min(weights.get('historical_discount',35),max(0,real or 0)*1.0)
    factors['minimum_proximity']=weights.get('minimum_proximity',20) if v and h['minimum'] and v<=h['minimum']*(1+th['near_min_percent']/100) else 0
    m90=h.get('median_90'); m30=h.get('median_30')
    factors['normalized_price']=min(weights.get('normalized_price',15),max(0,(m90-v)/m90*100)/2) if m90 and v else 0
    factors['preferred_category']=weights.get('preferred_category',10) if p.get('category') in c['preferred_categories'] else 0
    factors['watchlist']=weights.get('watchlist',10) if w is not None else 0
    factors['confidence']={'LOW':0,'MEDIUM':weights.get('medium_confidence',6),'HIGH':weights.get('high_confidence',10)}[h['confidence']]
    factors['risk_penalty']=-weights.get('suspicious_penalty',20) if len(flags)>int(h['confidence']=='LOW') else 0
    factors['history_penalty']=-weights.get('low_history_penalty',15) if h['confidence']=='LOW' else 0
    expiry=p.get('expiration_date'); short=bool(expiry and (datetime.fromisoformat(expiry).date()-datetime.fromisoformat(p['observed_at']).date()).days<=th.get('short_expiry_days',2))
    factors['shelf_life_penalty']=-weights.get('short_expiry_penalty',20) if short else 0
    score=round(max(0,min(100,sum(factors.values()))),2)
    label='IGNORE'
    excluded=blocked(p,c); available=p.get('availability')=='in_stock'
    priority_categories=set(c.get('preferred_categories',[]))
    for group in c.get('category_priorities',{}).values():priority_categories.update(group)
    relevant=p.get('category') in priority_categories or w is not None
    expired=bool(expiry and datetime.fromisoformat(expiry).date()<datetime.fromisoformat(p['observed_at']).date())
    if expired:available=False;flags.append('EXPIRATION DATE PASSED')
    end=p.get('promotion_end')
    if p.get('is_promo') and end and datetime.fromisoformat(end).date()<datetime.fromisoformat(p['observed_at']).date():
        available=False;flags.append('PROMOTION DATE PASSED')
    suspicious=any(x!='INSUFFICIENT HISTORY' for x in flags)
    if available and not excluded and v and relevant:
        if score>=th['watch_score'] or (w is not None and h['confidence']=='LOW'): label='WATCH'
        if score>=th['buy_score'] and real is not None and real>=th['buy_discount'] and m30 and (m30-v)/m30*100>=th['minimum_30_discount'] and not suspicious and h['confidence']!='LOW': label='BUY'
        if score>=th['strong_buy_score'] and real is not None and real>=th['strong_discount'] and h.get('min_90') and v<=h['min_90']*(1+th['near_min_percent']/100) and h['confidence']!='LOW' and not suspicious: label='STRONG BUY'
    if excluded: score=0; label='IGNORE'
    qty='BUY 1' if label in ('BUY','STRONG BUY') else ('WATCH' if label=='WATCH' else 'SKIP')
    reason='Conservative quantity: storage and expiry are not verified.'
    if qty=='BUY 1' and expiry and not short and p.get('storage_verified') and p.get('shelf_stable'):
        qty='BUY 2'; reason='Verified shelf-stable product and expiry provided.'
    preferred_qty=(w or {}).get('preferred_quantity',1)
    if qty=='BUY 2' and label=='STRONG BUY' and expiry and (datetime.fromisoformat(expiry).date()-datetime.fromisoformat(p['observed_at']).date()).days>=th.get('stock_up_min_expiry_days',30) and preferred_qty>=3:
        qty='STOCK UP';reason='Explicit preferred quantity, verified storage, expiry at least 30 days.'
    metrics={'current_price':p['price'],'current_normalized_price':v,'previous_observed_price':h['previous'],'price_change_percent':(v/h['previous']-1)*100 if v and h['previous'] else None,'percent_below_historical_median':(1-v/h['median'])*100 if v and h['median'] else None,'percent_above_historical_minimum':(v/h['minimum']-1)*100 if v and h['minimum'] else None,'baseline':baseline}
    return dict(metrics=metrics,preferred_purchase_quantity=preferred_qty if qty=='STOCK UP' else 2 if qty=='BUY 2' else 1 if qty=='BUY 1' else 0,score=score,classification=label,real_discount=real,displayed_discount=display,flags=flags,factors=factors,confidence=h['confidence'],quantity=qty,quantity_reason=reason,watchlist=w is not None,excluded=excluded,history=h)
