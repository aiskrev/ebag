"""Polite HTTP client for the anonymous public eBag storefront, limited to /offers.

The /offers page is a JavaScript app. Its products come from one public JSON
endpoint, the same request the page itself makes in a browser:
    GET /categories/offers-products/json?page=1&mode=all_offers
Pagination follows the `next` link of each response. Nothing else is crawled.
"""
import hashlib,json,logging,re,time,urllib.request,urllib.error,urllib.parse,urllib.robotparser
from datetime import datetime,timezone
from html.parser import HTMLParser
from pathlib import Path

OFFERS_PAGE='https://ebag.bg/offers'
OFFERS_PATH='/categories/offers-products/json'
REDIRECTS=(301,302,303,307,308)
TRANSIENT=(500,502,503,504)
MAX_REDIRECTS=5
PRODUCT_PATH=re.compile(r'^/[A-Za-z0-9-]+/\d+$')
log=logging.getLogger('ebag.http')

class PolicyError(RuntimeError): pass
class ReviewRequired(PolicyError):
    """A person must re-review terms/parser; retrying before that is pointless."""
class AccessDenied(PolicyError):
    """The store said stop (401/403/429/challenge); never retried the same day."""
class FetchError(RuntimeError):
    """An HTTP failure carrying what an operator needs. Never holds secrets."""
    def __init__(self,message,diag,transient):
        super().__init__(message);self.diag=diag;self.transient=transient

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
class KeepRedirect(urllib.request.HTTPRedirectHandler):
    """Surface 3xx as HTTPError so Client.get can validate the target itself."""
    def redirect_request(self,*args,**kwargs): return None

def canonical(url):
    u=urllib.parse.urlsplit(url)
    return u.scheme=='https' and u.netloc=='ebag.bg' and not u.username and not u.password

def offers_url(page=None):
    query={'mode':'all_offers'} if page is None else {'page':str(page),'mode':'all_offers'}
    return 'https://ebag.bg'+OFFERS_PATH+'?'+urllib.parse.urlencode(query)

def is_offers_page(url):
    """Only the offers listing endpoint, mode=all_offers, optional numeric page."""
    u=urllib.parse.urlsplit(url)
    if not canonical(url) or u.path!=OFFERS_PATH or u.fragment:return False
    q=urllib.parse.parse_qs(u.query,keep_blank_values=True)
    if set(q)-{'mode','page'} or q.get('mode')!=['all_offers']:return False
    return 'page' not in q or (len(q['page'])==1 and q['page'][0].isdigit() and int(q['page'][0])>=1)

def is_product_url(url):
    return bool(url) and canonical(url) and bool(PRODUCT_PATH.match(urllib.parse.urlsplit(url).path))

def dedupe_key(p):
    """Product ID, then canonical URL, then name + package + price."""
    if p.get('external_id'):return 'id:'+str(p['external_id'])
    if p.get('url'):return 'url:'+p['url']
    return 'name:'+' '.join(str(p.get(k) or '').casefold().strip() for k in ('name','package','price'))

class Client:
    def __init__(self,c,cache='data/cache'):
        self.c=c['crawl']; self.base=self.c['base_url']; self.last=0; self.root=Path(cache); self.root.mkdir(parents=True,exist_ok=True)
        self.opener=urllib.request.build_opener(KeepRedirect()); self.robot=None
        self.release_drift=None; self.parse_errors=[]; self.requests=0

    def record(self,**entry):
        """One structured line per HTTP attempt; URLs only, no headers or credentials."""
        self.requests+=1
        log.info(json.dumps(dict(ts=datetime.now(timezone.utc).isoformat(timespec='seconds'),method='GET',**entry),ensure_ascii=False))

    def get(self,url,policy=False,fresh=False,kind='api',op=''):
        if not canonical(url): raise PolicyError('Only canonical HTTPS ebag.bg URLs allowed')
        if not policy and (self.robot is None or not self.robot.can_fetch(self.c['user_agent'],url)): raise PolicyError('Robots policy denies URL')
        key=hashlib.sha256(url.encode()).hexdigest(); f=self.root/key
        if not policy and not fresh and f.exists() and time.time()-f.stat().st_mtime<3600:return f.read_text()
        timeout=self.c['timeout_seconds'];current=url;chain=[];attempt=0
        def diag(status,error):
            return dict(ts=datetime.now(timezone.utc).isoformat(timespec='seconds'),stage=op or kind,kind=kind,method='GET',url=current,requested_url=url,status=status,attempts=attempt,redirects=list(chain),timeout=timeout,error=error)
        while True:
            attempt+=1
            time.sleep(max(0,self.c['interval_seconds']-(time.monotonic()-self.last)))
            self.last=time.monotonic()
            req=urllib.request.Request(current,headers={'User-Agent':self.c['user_agent'],'Accept':'application/json,text/html,text/plain'})
            try:
                with self.opener.open(req,timeout=timeout) as response:
                    data=response.read(4_000_001)
                    status=getattr(response,'status',200)
                self.record(kind=kind,op=op,url=current,status=status,redirects=chain,attempt=attempt,timeout=timeout)
                if len(data)>4_000_000:raise PolicyError('Response size limit')
                text=data.decode('utf-8')
                if any(x in text.casefold() for x in ('captcha','cf-chl-','verify you are human')):
                    exc=AccessDenied('Challenge encountered; stopping');exc.diag=diag(status,'challenge page');raise exc
                if not policy:f.write_text(text)
                return text
            except urllib.error.HTTPError as e:
                status=e.code;headers=e.headers or {}
                if status in REDIRECTS:
                    location=headers.get('Location');target=urllib.parse.urljoin(current,location) if location else None
                    self.record(kind=kind,op=op,url=current,status=status,redirects=chain,attempt=attempt,timeout=timeout,location=target)
                    if not target:raise FetchError(f'HTTP {status} without Location',diag(status,'redirect without Location'),False) from e
                    if not canonical(target):
                        exc=PolicyError(f'Redirect outside ebag.bg: {urllib.parse.urlsplit(target).netloc or target}');exc.diag=diag(status,'redirect outside ebag.bg');raise exc from e
                    if len(chain)>=MAX_REDIRECTS:raise FetchError('Too many redirects',diag(status,'too many redirects'),False) from e
                    if not policy and not self.robot.can_fetch(self.c['user_agent'],target):raise PolicyError('Robots policy denies redirect target')
                    chain.append(target);current=target;attempt-=1;continue
                self.record(kind=kind,op=op,url=current,status=status,redirects=chain,attempt=attempt,timeout=timeout,error=str(e.reason)[:80])
                if status in (401,403,429):
                    retry_after=headers.get('Retry-After')
                    exc=AccessDenied(f'Access/rate limit {status}; stopping'+(f' (Retry-After: {retry_after})' if retry_after else ''))
                    exc.diag=dict(diag(status,f'HTTP {status}'),retry_after=retry_after);raise exc from e
                if status in TRANSIENT and attempt<3:time.sleep(2**attempt);continue
                raise FetchError(f'HTTP Error {status}: {e.reason} ({kind}: {current})',diag(status,f'HTTP {status} {e.reason}'),status in TRANSIENT) from e
            except (TimeoutError,urllib.error.URLError,ConnectionError) as e:
                reason=getattr(e,'reason',e);name=type(reason).__name__
                self.record(kind=kind,op=op,url=current,status=None,redirects=chain,attempt=attempt,timeout=timeout,error=name)
                if attempt<3:time.sleep(2**attempt);continue
                raise FetchError(f'{name} after {attempt} attempts ({kind}: {current})',diag(None,name),True) from e

    def check_policy(self):
        from datetime import date
        robots=self.get(self.base+'/robots.txt',True,kind='policy',op='robots.txt')
        if not robots.strip().casefold().startswith('user-agent:'):raise PolicyError('Invalid robots response')
        self.robot=urllib.robotparser.RobotFileParser(); self.robot.parse(robots.splitlines())
        delay=self.robot.crawl_delay(self.c['user_agent']);rate=self.robot.request_rate(self.c['user_agent'])
        if delay:self.c['interval_seconds']=max(delay,self.c['interval_seconds'])
        if rate:self.c['interval_seconds']=max(rate.seconds/rate.requests,self.c['interval_seconds'])
        if not self.c['policy_reviewed']:raise ReviewRequired('Terms review not completed')
        age=(date.today()-date.fromisoformat(self.c['policy_reviewed_at'])).days
        if not 0<=age<=self.c.get('policy_review_days',30):raise ReviewRequired('Terms review expired; re-review public terms')
        # Always fresh: a cached page would hide a release that happened in the last hour.
        terms=self.get(self.base+'/terms',fresh=True,kind='policy',op='terms')
        # This detects storefront releases, not all dynamic terms edits. TTL also applies.
        match=re.search(r'window.STORE_COMMIT_HASH\s*=\s*"([a-f0-9]+)"',terms)
        if not match:raise ReviewRequired('Storefront release marker missing; inspect terms and parser')
        if match[1]!=self.c.get('reviewed_store_commit'):
            if self.c.get('on_release_change','stop')!='warn':raise ReviewRequired(f'Storefront release changed to {match[1]}; inspect terms and parser')
            self.release_drift=match[1]
        if not self.c['parser_validated']:raise ReviewRequired('Parser not validated')

    def adapt_or_record(self,raw):
        """One malformed product must not cost the whole day of observations."""
        from .ebag_source import adapt
        try:return adapt(raw)
        except (KeyError,ValueError,TypeError,AttributeError,ArithmeticError) as exc:
            self.parse_errors.append({'id':raw.get('id') if isinstance(raw,dict) else None,'error':repr(exc)[:200]})
            return None
    def check_drift(self,parsed):
        """Isolated bad rows are tolerated; widespread failure means the schema moved."""
        failed=len(self.parse_errors);ratio=self.c.get('max_parse_error_ratio',.05)
        if failed>=5 and failed>ratio*(failed+parsed):raise PolicyError(f'Listing schema drift: {failed} products failed to parse')

    def collect(self):
        """Every offer listed by /offers, starting at page 1 each day.

        Nothing is carried over from earlier days: offers change daily, and a
        stored `?page=N` disappears when the list shrinks. That stale page
        returned 404 and failed whole crawls (see docs/VALIDATION.md).
        """
        self.check_policy()
        products={};seen=set();url=offers_url(1);pages=0;expected=None;stopped=None;duplicates=0;invalid_urls=[]
        while url and pages<self.c['max_pages']:
            if url in seen:raise PolicyError(f'Offers pagination loops back to {url}')
            seen.add(url);kind='offers_page' if pages==0 else 'pagination'
            try:text=self.get(url,kind=kind,op=f'оферти, страница {pages+1}')
            except FetchError as exc:
                if pages and exc.diag.get('status') in (404,410):
                    # The list shrank while we paged through it; what we have is still valid.
                    stopped=dict(url=url,status=exc.diag['status']);break
                raise
            try:payload=json.loads(text)
            except ValueError:
                exc=PolicyError(f'Offers response is not JSON ({kind}); the page structure changed');exc.diag=dict(stage=kind,url=url,status=200,error='invalid JSON');raise exc
            if not isinstance(payload,dict) or not isinstance(payload.get('results'),list):
                exc=PolicyError('Offers response has no results list; the page structure changed');exc.diag=dict(stage=kind,url=url,status=200,error='missing results');raise exc
            if pages==0:expected=payload.get('count') if isinstance(payload.get('count'),int) else None
            pages+=1
            for raw in payload['results']:
                p=self.adapt_or_record(raw)
                if not p:continue
                if not is_product_url(p.get('url')):
                    invalid_urls.append(p.get('external_id'));continue
                p['source']='offers';p['source_url']=url
                key=dedupe_key(p)
                if key in products:duplicates+=1;continue
                products[key]=p
            nxt=payload.get('next');url=None
            if nxt:
                target=urllib.parse.urljoin(OFFERS_PAGE,nxt)
                if not is_offers_page(target):raise PolicyError(f'Unexpected pagination target {target}')
                url=target
        self.check_drift(len(products))
        if invalid_urls and not products:raise PolicyError('All product URLs are invalid')
        warnings=[]
        if url:warnings.append(f'Лимитът от {self.c["max_pages"]} страници е достигнат; останалите оферти не са обходени.')
        if expected and len(products)<.9*expected and not url:
            warnings.append(f'Обработени са {len(products)} от обявени {expected} оферти.')
        self.stats={'source':OFFERS_PAGE,'listing_pages':pages,'offers_count':expected,'unique_offers':len(products),'duplicates':duplicates,
                    'listing_truncated':bool(url),'pagination_stopped':stopped,'invalid_urls':len(invalid_urls),'requests':self.requests,
                    'parse_errors':self.parse_errors[:20],'parse_error_count':len(self.parse_errors),'release_drift':self.release_drift,'warnings':warnings}
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
