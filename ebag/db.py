import json, sqlite3
from pathlib import Path
from .core import now
SCHEMA='''
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS crawl_runs(id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT, products_scanned INTEGER DEFAULT 0, products_added INTEGER DEFAULT 0, products_updated INTEGER DEFAULT 0, products_unavailable INTEGER DEFAULT 0, parser_errors INTEGER DEFAULT 0, http_errors INTEGER DEFAULT 0, duration_seconds REAL, details_json TEXT);
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, identity_hash TEXT UNIQUE NOT NULL, external_product_id TEXT, sku TEXT, original_name TEXT, normalized_name TEXT, brand TEXT, category TEXT, subcategory TEXT, package_quantity REAL, package_unit TEXT, package_count INTEGER, product_url TEXT, image_url TEXT, first_seen_at TEXT, last_seen_at TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS price_history(id INTEGER PRIMARY KEY, product_id INTEGER REFERENCES products(id), crawl_run_id INTEGER REFERENCES crawl_runs(id), observed_at TEXT, current_price_eur REAL, regular_price_eur REAL, promotional_price_eur REAL, displayed_discount_percent REAL, calculated_discount_percent REAL, normalized_price_eur REAL, normalized_unit TEXT, availability TEXT, promotion_start TEXT, promotion_end TEXT, expiration_date TEXT, parser_version TEXT, raw_data_json TEXT, UNIQUE(product_id,crawl_run_id));
CREATE INDEX IF NOT EXISTS idx_history_product_date ON price_history(product_id,observed_at);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY, product_id INTEGER REFERENCES products(id), sent_at TEXT, notification_type TEXT, price_at_notification REAL, deal_score REAL, recipient TEXT, message_hash TEXT, status TEXT, UNIQUE(recipient,message_hash));
CREATE INDEX IF NOT EXISTS idx_notification_product_recipient ON notifications(product_id,recipient,sent_at);
CREATE TABLE IF NOT EXISTS product_matches(id INTEGER PRIMARY KEY, source_product_id INTEGER REFERENCES products(id), candidate_product_id INTEGER REFERENCES products(id), match_type TEXT, match_confidence REAL, reviewed INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS system_health(id INTEGER PRIMARY KEY, checked_at TEXT, last_successful_crawl TEXT, database_status TEXT, email_status TEXT, crawler_status TEXT, details_json TEXT);
'''
class Repository:
    def __init__(self,path):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.conn=sqlite3.connect(path); self.conn.row_factory=sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON'); self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript(SCHEMA)
        self.conn.execute('INSERT OR IGNORE INTO schema_migrations VALUES(1,?)',(now(),)); self.conn.commit()
    def start(self):
        cur=self.conn.execute('INSERT INTO crawl_runs(started_at,status) VALUES(?,?)',(now(),'running')); self.conn.commit(); return cur.lastrowid
    def save(self,p,run):
        old=self.conn.execute('SELECT id FROM products WHERE identity_hash=?',(p['identity_hash'],)).fetchone()
        if old:
            pid=old[0]
            self.conn.execute('UPDATE products SET last_seen_at=?,active=1,original_name=?,normalized_name=?,brand=?,category=?,subcategory=?,product_url=?,image_url=? WHERE id=?',(p['observed_at'],p['name'],p['normalized_name'],p.get('brand'),p.get('category'),p.get('subcategory'),p.get('url'),p.get('image_url'),pid))
        else:
            cols=['identity_hash','external_product_id','sku','original_name','normalized_name','brand','category','subcategory','package_quantity','package_unit','package_count','product_url','image_url','first_seen_at','last_seen_at']
            vals=[p['identity_hash'],p.get('external_id'),p.get('sku'),p['name'],p['normalized_name'],p.get('brand'),p.get('category'),p.get('subcategory'),p['quantity'],p['unit'],p['package_count'],p.get('url'),p.get('image_url'),p['observed_at'],p['observed_at']]
            pid=self.conn.execute('INSERT INTO products('+','.join(cols)+') VALUES('+','.join('?' for _ in cols)+')',vals).lastrowid
        self.conn.execute('INSERT INTO price_history(product_id,crawl_run_id,observed_at,current_price_eur,regular_price_eur,promotional_price_eur,displayed_discount_percent,calculated_discount_percent,normalized_price_eur,normalized_unit,availability,promotion_start,promotion_end,expiration_date,parser_version,raw_data_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(pid,run,p['observed_at'],p['price'],p.get('regular_price'),p['price'] if p.get('regular_price') else None,p.get('displayed_discount'),((p['regular_price']-p['price'])/p['regular_price']*100) if p.get('regular_price') else None,p['normalized_price'],p['unit'],p.get('availability'),p.get('promotion_start'),p.get('promotion_end'),p.get('expiration_date'),p.get('parser_version','import-0.2'),json.dumps(p['raw'],ensure_ascii=False)))
        return pid,old is None
    def history(self,pid,unit):
        return [dict(observed_at=r['observed_at'],normalized_price=r['normalized_price_eur'],availability=r['availability'],is_promo=bool(json.loads(r['raw_data_json']).get('is_promo')),regular_price=r['regular_price_eur'],current_price=r['current_price_eur']) for r in self.conn.execute('SELECT * FROM price_history WHERE product_id=? AND normalized_unit=? ORDER BY observed_at',(pid,unit))]
    def finish(self,run,status,scanned,added,errors=0,detail=None):
        self.conn.execute("UPDATE crawl_runs SET products_unavailable=(SELECT count(*) FROM price_history WHERE crawl_run_id=? AND availability='out_of_stock') WHERE id=?",(run,run))
        self.conn.execute("UPDATE crawl_runs SET finished_at=?,status=?,products_scanned=?,products_added=?,products_updated=?,parser_errors=?,details_json=?,duration_seconds=(julianday(?)-julianday(started_at))*86400 WHERE id=?",(now(),status,scanned,added,scanned-added,errors,json.dumps(detail),now(),run)); self.conn.commit()
