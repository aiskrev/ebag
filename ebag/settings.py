import json,os,re
from pathlib import Path
from datetime import datetime,date
from zoneinfo import ZoneInfo

def load_env(path='.env'):
    f=Path(path)
    if not f.exists():return
    for line in f.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):continue
        key,sep,value=line.partition('=');key=key.strip()
        if not sep or not re.fullmatch('[A-Z][A-Z0-9_]*',key):raise ValueError('Invalid .env key')
        os.environ.setdefault(key,value.strip().strip('"').strip("'"))

def load(path):
    text=Path(path).read_text()
    try:c=json.loads(text)
    except json.JSONDecodeError:
        try:import yaml
        except ImportError as exc:raise ValueError('Use JSON syntax or install package with PyYAML') from exc
        c=yaml.safe_load(text)
    for key,typ in [('excluded_categories',list),('excluded_products',list),('preferred_categories',list),('excluded_keywords',list),('watchlist',list),('notification_email',list),('crawl',dict),('deal_thresholds',dict),('history',dict)]:
        if not isinstance(c.get(key),typ):raise ValueError('Invalid/missing config field '+key)
    ZoneInfo(c['timezone']);datetime.strptime(c['daily_crawl_time'],'%H:%M')
    cr=c['crawl'];th=c['deal_thresholds'];h=c['history']
    for key,low,high in [('max_pages',1,100),('interval_seconds',2,3600),('timeout_seconds',1,120),('max_tracked_refresh',0,1000)]:
        v=cr.get(key)
        if isinstance(v,bool) or not isinstance(v,(int,float)) or not low<=v<=high:raise ValueError('Invalid crawl limit '+key)
    if cr['base_url']!='https://ebag.bg':raise ValueError('Unsupported base URL')
    if not 0<=th['watch_score']<=th['buy_score']<=th['strong_buy_score']<=100:raise ValueError('Threshold order invalid')
    for key in ('minimum_real_discount_for_email','buy_discount','strong_discount','minimum_30_discount','resend_drop_percent','resend_score_increase','near_min_percent'):
        if not 0<=th[key]<=100:raise ValueError('Invalid deal threshold '+key)
    if not 1<=th['cooldown_days']<=365:raise ValueError('Invalid cooldown')
    if not 0<h['window_coverage']<=1:raise ValueError('Invalid historical coverage')
    for key in ('high_observations','high_days','medium_observations','medium_days'):
        if not isinstance(h[key],int) or h[key]<1:raise ValueError('Invalid history threshold '+key)
    if not h['high_days']>=h['medium_days'] or not h['high_observations']>=h['medium_observations']:raise ValueError('Confidence thresholds out of order')
    for recipient in c['notification_email']:
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',recipient):raise ValueError('Invalid recipient')
    for w in c['watchlist']:
        if not isinstance(w,dict) or not isinstance(w.get('keywords'),str) or not w['keywords'].strip():raise ValueError('Watchlist keywords required')
        if w.get('unit') not in (None,'kg','liter','piece'):raise ValueError('Invalid watchlist unit')
        for key in ('min_quantity','max_quantity','max_price','max_unit_price','preferred_quantity'):
            if key in w and (not isinstance(w[key],(int,float)) or w[key]<=0):raise ValueError('Invalid watchlist bound '+key)
        if w.get('min_quantity',0)>w.get('max_quantity',float('inf')):raise ValueError('Watchlist quantity bounds reversed')
    return c
