"""Candidate matching only: histories are never silently merged."""
import re
from difflib import SequenceMatcher
from .core import normalized,now

def family(name):
    return normalized(re.sub(r'(?:(?:\d+)\s*[xх×]\s*)?\d+(?:[.,]\d+)?\s*(?:kg|кг|g|гр|г|ml|мл|l|л|liter|литра|pcs|бр)\b','',name,flags=re.I))
def compare(a,b):
    if a['identity_hash']==b['identity_hash']:return 'EXACT SAME PRODUCT',1.0
    same_id=bool(a.get('external_id') and a.get('external_id')==b.get('external_id'))
    brand=bool(a.get('brand') and b.get('brand') and normalized(a['brand'])==normalized(b['brand']))
    category=bool(a.get('category') and a.get('category')==b.get('category'))
    if a.get('brand') and b.get('brand') and not brand:return 'DIFFERENT PRODUCT',0.0
    if a.get('unit')!=b.get('unit'):return 'DIFFERENT PRODUCT',0.0
    ratio=SequenceMatcher(None,family(a['name']),family(b['name'])).ratio()
    changed=a.get('quantity')!=b.get('quantity')
    if (same_id or (brand and category and ratio>=.95)) and changed:return 'SAME PRODUCT DIFFERENT PACKAGE',.98 if same_id else .90
    if same_id and not changed:return 'RENAMED PRODUCT',.95
    if brand and category and ratio>=.85:return 'PROBABLY SAME PRODUCT',.8
    return 'UNKNOWN',round(ratio*.5,2)
def shrink(old,new,confidence):
    if old.get('variable_weight') or new.get('variable_weight'):return None
    if not old.get('quantity') or not new.get('quantity') or old.get('unit')!=new.get('unit'):return None
    if new['quantity']>=old['quantity'] or not old.get('normalized_price') or not new.get('normalized_price'):return None
    if abs(new['price']/old['price']-1)>.1:return None
    if new['normalized_price']<=old['normalized_price']:return None
    return {'label':'POSSIBLE SHRINKFLATION','package_change_percent':(new['quantity']/old['quantity']-1)*100,'normalized_price_increase_percent':(new['normalized_price']/old['normalized_price']-1)*100,'confidence':'HIGH' if confidence>=.98 else 'MEDIUM' if confidence>=.9 else 'LOW'}
def candidates(repo,pid,p,c):
    rows=repo.conn.execute('SELECT * FROM products WHERE id<>? AND (external_product_id=? OR brand=?) LIMIT 200',(pid,p.get('external_id'),p.get('brand'))).fetchall()
    result=[]
    for row in rows:
        old=repo.conn.execute('SELECT * FROM price_history WHERE product_id=? AND observed_at<? ORDER BY observed_at DESC LIMIT 1',(row['id'],p['observed_at'])).fetchone()
        if not old:continue
        a={'identity_hash':row['identity_hash'],'external_id':row['external_product_id'],'name':row['original_name'],'brand':row['brand'],'category':row['category'],'quantity':row['package_quantity'],'unit':row['package_unit'],'price':old['current_price_eur'],'normalized_price':old['normalized_price_eur']}
        kind,conf=compare(a,p)
        if kind in ('DIFFERENT PRODUCT','UNKNOWN'):continue
        reviewed=False
        for manual in c.get('manual_matches',[]):
            if manual.get('source_id')==row['id'] and manual.get('candidate_id')==pid:
                kind=manual['match_type'];conf=float(manual['confidence']);reviewed=True
        prev=repo.conn.execute('SELECT id FROM product_matches WHERE source_product_id=? AND candidate_product_id=?',(row['id'],pid)).fetchone()
        if prev:repo.conn.execute('UPDATE product_matches SET match_type=?,match_confidence=?,reviewed=? WHERE id=?',(kind,conf,int(reviewed),prev[0]))
        else:repo.conn.execute('INSERT INTO product_matches(source_product_id,candidate_product_id,match_type,match_confidence,reviewed,created_at) VALUES(?,?,?,?,?,?)',(row['id'],pid,kind,conf,int(reviewed),now()))
        result.append({'source_product_id':row['id'],'candidate_product_id':pid,'match_type':kind,'match_confidence':conf,'reviewed':reviewed,'shrinkflation':shrink(a,p,conf) if kind=='SAME PRODUCT DIFFERENT PACKAGE' else None})
    return result
