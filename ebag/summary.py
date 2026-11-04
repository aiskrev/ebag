"""Daily digest of what the crawl saw. Deal alerts stay separate and unchanged.

The digest reports observations, never recommendations, so it does not weaken
the cold-start rules. It also works as a heartbeat: no morning mail means the
crawl did not finish.
"""
import hashlib
import html
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .core import now, watch
from .notify import eligible, recipients

REVIEW_WARNING_DAYS = 7
MINIMUM_DROP_PERCENT = 5


def change(d):
    return (d.get('metrics') or {}).get('price_change_percent')


def cheapest_by_watch(items, c):
    """The lowest unit price per watchlist entry, in that entry's most common unit.

    A price per piece and a price per kg cannot be compared, so the smaller
    number alone would name the wrong product.
    """
    groups = {}
    for _, p, d in items:
        w = watch(p, c)
        if w and not d['excluded'] and p.get('normalized_price') is not None:
            groups.setdefault(w['keywords'], {}).setdefault(p.get('unit'), []).append((p, d))
    cheapest = {}
    for keyword, units in groups.items():
        common = max(units.values(), key=len)
        cheapest[keyword] = min(common, key=lambda x: x[0]['normalized_price'])
    return cheapest


def biggest_drops(items, c, limit):
    relevant = set(c.get('preferred_categories', []))
    drops = []
    for _, p, d in items:
        percent = change(d)
        if percent is None or percent > -MINIMUM_DROP_PERCENT or d['excluded'] or p.get('availability') != 'in_stock':
            continue
        if p.get('category') in relevant or d.get('watchlist'):
            drops.append((percent, p))
    return sorted(drops, key=lambda x: x[0])[:limit]


def price(p):
    return f"{p['price']:.2f} EUR ({p['normalized_price']:.2f} EUR/{p['unit']})"


def review_warning(c, today):
    crawl = c['crawl']
    ends = date.fromisoformat(crawl['policy_reviewed_at']) + timedelta(days=crawl.get('policy_review_days', 30))
    left = (ends - today).days
    if left <= REVIEW_WARNING_DAYS:
        return f'Внимание: прегледът на условията важи до {ends:%d.%m.%Y}. След това обходите спират, докато не бъде подновен.'
    return None


def lines(items, c, stats, history_days, today):
    coverage = stats.get('coverage') or {}
    deals = sum(1 for _, p, d in items if eligible(p, d, c))
    days = '1 ден' if history_days == 1 else f'{history_days} дни'
    out = [f"Обход: {stats['products']} продукта, {stats['errors']} грешки при разчитане, история: {days}."]
    if coverage.get('listing_truncated'):
        out.append('Каталогът не е обходен докрай; останалите страници са за следващия обход.')
    if stats['errors']:
        out.append('Заради грешките при разчитане днес няма да има писма с оферти.')
    warning = review_warning(c, today)
    if warning:
        out.append(warning)
    if coverage.get('release_drift'):
        out.append(f"Внимание: storefront release {coverage['release_drift']} още не е прегледан.")
    if deals:
        out.append(f'Оферти, които отговарят на условията: {deals}. Те се пращат с отделно писмо.')
    elif history_days < c['history']['medium_days']:
        out.append(f"Няма доказани оферти. Историята е кратка: оценки са възможни след около {c['history']['medium_days']} дни обходи.")
    else:
        out.append('Днес няма цена, която да е реално ниска спрямо историята.')
    limit = c.get('daily_summary', {}).get('max_drops', 5)
    drops = biggest_drops(items, c, limit)
    if drops:
        out.append('Най-големи спадове спрямо предишното наблюдение:')
        out += [f"{percent:.1f}%: {p['name']}, {price(p)} {p.get('url', '')}" for percent, p in drops]
    elif history_days < 2:
        out.append('Сравнение с предишен ден ще има от следващия обход.')
    else:
        out.append(f'Няма спад от поне {MINIMUM_DROP_PERCENT}% в следените категории.')
    cheapest = cheapest_by_watch(items, c)
    if cheapest:
        out.append('Watchlist, най-ниска единична цена в момента:')
        for keyword, (p, d) in cheapest.items():
            percent = change(d)
            delta = f', {percent:+.1f}% спрямо предишното наблюдение' if percent else ''
            out.append(f"{keyword}: {p['name']}, {price(p)}{delta} {p.get('url', '')}")
    out.append('Това е резюме на наблюдения, не препоръка за покупка.')
    return out


def local_today(c):
    return datetime.now(ZoneInfo(c['timezone'])).date()


def history_days(repo):
    return repo.conn.execute('SELECT count(DISTINCT substr(observed_at,1,10)) FROM price_history').fetchone()[0]


def send(repo, items, c, provider, stats):
    """At most one digest per local day. Raises on transport failure."""
    if not c.get('daily_summary', {}).get('enabled', True):
        return 0
    today = local_today(c)
    pending = []
    for recipient in recipients(c):
        fingerprint = hashlib.sha256(json.dumps(['summary', today.isoformat()]).encode()).hexdigest()
        if not repo.conn.execute('SELECT 1 FROM notifications WHERE recipient=? AND message_hash=?', (recipient, fingerprint)).fetchone():
            pending.append((recipient, fingerprint))
    if not pending:
        return 0
    if hasattr(provider, 'validate'):
        provider.validate()
    body = lines(items, c, stats, history_days(repo), today)
    subject = f'eBag: дневно резюме за {today:%d.%m.%Y}'
    markup = '<html><body>' + ''.join('<p>' + html.escape(x) + '</p>' for x in body) + '</body></html>'
    for recipient, fingerprint in pending:
        provider.send(recipient, subject, '\n\n'.join(body), markup)
        repo.conn.execute('INSERT INTO notifications(sent_at,notification_type,recipient,message_hash,status) VALUES(?,?,?,?,?)', (now(), 'DAILY_SUMMARY', recipient, fingerprint, 'sent'))
        repo.conn.commit()
    return len(pending)
