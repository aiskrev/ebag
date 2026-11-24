"""Daily offers report, sent after every finished crawl. Deal alerts stay separate.

It lists what /offers shows today, never recommendations, so it does not weaken
the cold-start rules. It also works as a heartbeat: no morning mail means the
crawl did not finish.
"""
import hashlib
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import emails
from .core import now, watch
from .notify import eligible, recipients

REVIEW_WARNING_DAYS = 7


class ProvenanceError(RuntimeError):
    """The report would contain items not proven to come from /offers."""


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


def price(p):
    return f"{p['price']:.2f} EUR ({p['normalized_price']:.2f} EUR/{p['unit']})"


def review_warning(c, today):
    crawl = c['crawl']
    ends = date.fromisoformat(crawl['policy_reviewed_at']) + timedelta(days=crawl.get('policy_review_days', 30))
    if (ends - today).days <= REVIEW_WARNING_DAYS:
        return f'Прегледът на условията важи до {ends:%d.%m.%Y}. След това обходите спират, докато не бъде подновен.'
    return None


def check_provenance(items):
    outside = [p.get('external_id') for _, p, _ in items if p.get('source') != 'offers' or not p.get('source_url')]
    if outside:
        raise ProvenanceError(f'Report refused: {len(outside)} items are not proven to come from https://ebag.bg/offers')


def build(items, c, stats, today):
    """(subject, text, html) for the report, or a diagnostic when there are no offers."""
    check_provenance(items)
    coverage = stats.get('coverage') or {}
    warnings = list(stats.get('warnings') or [])
    for extra in (review_warning(c, today),
                  f"Storefront release {coverage['release_drift'][:7]} не е прегледан ръчно." if coverage.get('release_drift') else None,
                  'Заради грешките при разчитане днес няма писма с оферти.' if stats.get('errors') else None):
        if extra:
            warnings.append(extra)
    meta = dict(started_at=stats.get('started_at'), status=stats.get('status'), found=coverage.get('offers_count'),
                processed=stats.get('products'), skipped=stats.get('errors', 0),
                hidden=sum(1 for _, p, d in items if d.get('excluded')),
                deals=sum(1 for _, p, d in items if eligible(p, d, c)), warnings=warnings)
    rendered = emails.render_offers(items, meta, c.get('daily_summary', {}).get('max_rows', 50))
    subject, text, markup = rendered or emails.render_empty(meta)
    return f'{subject}, {today:%d.%m.%Y}', text, markup


def local_today(c):
    return datetime.now(ZoneInfo(c['timezone'])).date()


def send(repo, items, c, provider, stats):
    """At most one report per local day. Raises on transport failure."""
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
    subject, text, markup = build(items, c, stats, today)
    if hasattr(provider, 'validate'):
        provider.validate()
    for recipient, fingerprint in pending:
        provider.send(recipient, subject, text, markup)
        repo.conn.execute('INSERT INTO notifications(sent_at,notification_type,recipient,message_hash,status) VALUES(?,?,?,?,?)', (now(), 'DAILY_SUMMARY', recipient, fingerprint, 'sent'))
        repo.conn.commit()
    return len(pending)
