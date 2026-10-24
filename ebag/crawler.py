"""Conservative public JSON-LD adapter. Live eBag compatibility is NOT validated."""
import hashlib,json,time,urllib.request,urllib.error,urllib.parse,urllib.robotparser
from html.parser import HTMLParser
from pathlib import Path
class PolicyError(RuntimeError): pass
class Scripts(HTMLParser):
    def __init__(self): super().__init__(); self.capture=False; self.buf=[]; self.scripts=[]; self.links=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='script' and a.get('type')=='application/ld+json': self.capture=True; self.buf=[]
        if tag=='a' and a.get('href'): self.links.append(a['href'])
    def handle_data(self,data):
        if self.capture:self.buf.append(data)
    def handle_endtag(self,tag):
        if tag=='script' and self.capture:self.scripts.append(''.join(self.buf)); self.capture=False
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): raise PolicyError('Redirect blocked; review canonical seed URL')
class Client:
    def __init__(self,c,cache='data/cache'):
        self.c=c['crawl']; self.base=self.c['base_url']; self.last=0; self.root=Path(cache); self.root.mkdir(parents=True,exist_ok=True)
        self.opener=urllib.request.build_opener(NoRedirect()); self.robot=None
    def get(self,url,policy=False):
        u=urllib.parse.urlsplit(url)
        if u.scheme!='https' or u.netloc!='ebag.bg' or u.username or u.password: raise PolicyError('Only canonical HTTPS ebag.bg URLs allowed')
        if not policy and (self.robot is None or not self.robot.can_fetch(self.c['user_agent'],url)): raise PolicyError('Robots policy denies URL')
        key=hashlib.sha256(url.encode()).hexdigest(); f=self.root/key
        if not policy and f.exists() and time.time()-f.stat().st_mtime<3600:return f.read_text()
        for attempt in range(3):
            time.sleep(max(0,self.c['interval_seconds']-(time.monotonic()-self.last)))
            self.last=time.monotonic()
            req=urllib.request.Request(url,headers={'User-Agent':self.c['user_agent'],'Accept':'application/json,text/html,text/plain'})
            try:
                with self.opener.open(req,timeout=self.c['timeout_seconds']) as response:
                    data=response.read(4_000_001)
                    if len(data)>4_000_000:raise PolicyError('Response size limit')
                    text=data.decode('utf-8')
                    if any(x in text.casefold() for x in ('captcha','cf-chl-','verify you are human')): raise PolicyError('Challenge encountered; stopping')
                    if not policy:f.write_text(text)
                    return text
            except urllib.error.HTTPError as e:
                if e.code in (401,403,429): raise PolicyError(f'Access/rate limit {e.code}; stopping') from e
                if e.code<500 or attempt==2:raise
            except (TimeoutError,urllib.error.URLError):
                if attempt==2:raise
            time.sleep(2**attempt)

    def check_policy(self):
        from datetime import date
        import re
        robots=self.get(self.base+'/robots.txt',True)
        if not robots.strip().casefold().startswith('user-agent:'):raise PolicyError('Invalid robots response')
        self.robot=urllib.robotparser.RobotFileParser(); self.robot.parse(robots.splitlines())
        delay=self.robot.crawl_delay(self.c['user_agent']);rate=self.robot.request_rate(self.c['user_agent'])
        if delay:self.c['interval_seconds']=max(delay,self.c['interval_seconds'])
        if rate:self.c['interval_seconds']=max(rate.seconds/rate.requests,self.c['interval_seconds'])
        if not self.c['policy_reviewed']:raise PolicyError('Terms review not completed')
        age=(date.today()-date.fromisoformat(self.c['policy_reviewed_at'])).days
        if not 0<=age<=self.c.get('policy_review_days',30):raise PolicyError('Terms review expired; re-review public terms')
        terms=self.get(self.base+'/terms')
        # This detects storefront releases, not all dynamic terms edits. TTL also applies.
        match=re.search(r'window.STORE_COMMIT_HASH\s*=\s*"([a-f0-9]+)"',terms)
        if not match or match[1]!=self.c.get('reviewed_store_commit'):raise PolicyError('Storefront release changed; inspect terms and parser')
        if not self.c['parser_validated']:raise PolicyError('Parser not validated')
    def save_state(self):
        temp=self.state_path.with_suffix('.tmp');temp.write_text(json.dumps(self.pending_frontier));temp.replace(self.state_path)
    def collect(self,tracked_ids=()):
        from .ebag_source import adapt
        self.check_policy();products={};page_errors=[]
        seeds=[self.base+'/categories/offers-products/json?mode=all_offers']+list(self.c.get('category_seeds',[]))
        # Each category seed gets a first page before additional pagination.
        state_key=hashlib.sha256(json.dumps(seeds).encode()).hexdigest()[:12]
        self.state_path=self.root.parent/f'frontier-{state_key}.json'
        carry=json.loads(self.state_path.read_text()) if self.state_path.exists() else []
        queue=list(seeds)+carry;seen=set();page_count=0;remaining=False
        while queue and page_count<self.c['max_pages']:
            url=queue.pop(0)
            if url in seen:continue
            seen.add(url);page_count+=1
            payload=json.loads(self.get(url))
            if not isinstance(payload,dict) or not isinstance(payload.get('results'),list):raise PolicyError('Listing schema drift')
            for raw in payload['results']:
                p=adapt(raw);products[p['external_id']]=p
            nxt=payload.get('next')
            if nxt:
                target=urllib.parse.urljoin(url,nxt)
                if urllib.parse.urlsplit(target).path!=urllib.parse.urlsplit(url).path:raise PolicyError('Unexpected pagination target')
                queue.append(target)
        self.pending_frontier=list(dict.fromkeys(x for x in queue if x not in seen))
        remaining=bool(self.pending_frontier)
        ids=list(dict.fromkeys(str(x) for x in tracked_ids if str(x) not in products))
        refresh_limit=self.c.get('max_tracked_refresh',100)
        for pid in ids[:refresh_limit]:
            if not pid.isdigit():continue
            url=self.base+f'/products/{pid}/json'
            try:payload=json.loads(self.get(url))
            except urllib.error.HTTPError as exc:
                if exc.code==404:page_errors.append({'product_id':pid,'status':404});continue
                raise
            p=adapt(payload);products[p['external_id']]=p
        self.stats={'listing_pages':page_count,'listing_truncated':remaining,'tracked_pending':max(0,len(ids)-refresh_limit),'missing_products':page_errors}
        if not products:raise PolicyError('No products parsed; crawl is not successful')
        return list(products.values())

def parse_jsonld(scripts,url):
    result=[]
    def visit(obj):
        if isinstance(obj,list):
            for x in obj:visit(x)
        elif isinstance(obj,dict):
            types=obj.get('@type',[]); types=[types] if isinstance(types,str) else types
            if 'Product' in types:
                offer=obj.get('offers',{}); offer=offer[0] if isinstance(offer,list) and offer else offer
                if not isinstance(offer,dict) or offer.get('price') is None:return
                brand=obj.get('brand'); brand=brand.get('name') if isinstance(brand,dict) else brand
                result.append(dict(name=obj['name'],external_id=obj.get('productID') or obj.get('sku'),sku=obj.get('sku'),brand=brand,category=obj.get('category'),url=urllib.parse.urljoin(url,obj.get('url') or url),image_url=obj.get('image'),price=offer['price'],currency=offer.get('priceCurrency'),availability='in_stock' if str(offer.get('availability','')).rsplit('/',1)[-1]=='InStock' else 'unknown',source_jsonld=obj))
            for value in obj.values():
                if isinstance(value,(dict,list)):visit(value)
    for script in scripts:visit(json.loads(script))
    return result
