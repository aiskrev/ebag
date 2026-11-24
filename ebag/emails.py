"""Email-safe HTML: tables, inline styles, no scripts or external assets.

Kept small on purpose: Gmail clips messages over about 100 KB, so the offers
table is capped and the rest is counted, not dropped silently.
"""
import html
from datetime import datetime
from zoneinfo import ZoneInfo

DISCLAIMER = 'Това е автоматично генерирано служебно съобщение и не представлява препоръка за покупка.'
NO_DATA = 'Няма данни'
OFFERS_TITLE = 'eBag оферти – дневен отчет'
FAILURE_TITLE = 'eBag агент: Дневният обход не успя'
SOURCE = 'https://ebag.bg/offers'
UNITS = {'kg': 'кг', 'liter': 'л', 'piece': 'бр'}
AVAILABILITY = {'in_stock': 'В наличност', 'out_of_stock': 'Няма наличност'}
STATUS = {'success': 'Успешен', 'partial': 'Частично успешен', 'failed': 'Неуспешен'}
FONT = 'font-family:Arial,Helvetica,sans-serif'
INK = '#1a1a1a'
HEAD = '#0b5d34'
CELL = f'padding:7px 8px;border-bottom:1px solid #d9d9d9;{FONT};font-size:13px;color:{INK};vertical-align:top'
ROW_CELL = f'padding:6px;border-bottom:1px solid #ddd;{FONT};font-size:13px;color:{INK}'


def esc(value):
    return html.escape('' if value is None else str(value), quote=True)


def number(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float('inf'), float('-inf')) else None


def money(value):
    v = number(value)
    return NO_DATA if v is None else f'{v:.2f}'.replace('.', ',') + ' €'


def discount(p):
    """(percent, absolute). Store's figure first; otherwise computed from valid prices only."""
    new, old = number(p.get('price')), number(p.get('regular_price'))
    valid = old is not None and new is not None and old > 0 and new >= 0 and old > new
    absolute = old - new if valid else None
    shown = number(p.get('displayed_discount'))
    if shown is not None and 0 < shown < 100:
        return shown, absolute
    return ((old - new) / old * 100 if valid else None), absolute


def percent(value):
    return NO_DATA if value is None else f'{value:.0f}%'


def unit_price(p):
    v = number(p.get('normalized_price'))
    return NO_DATA if v is None else money(v) + '/' + UNITS.get(p.get('unit'), p.get('unit') or '?')


def package(p):
    return p.get('original_package_label') or p.get('package') or NO_DATA


def local_time(iso, zone='Europe/Sofia'):
    try:
        return datetime.fromisoformat(iso).astimezone(ZoneInfo(zone)).strftime('%d.%m.%Y %H:%M')
    except (TypeError, ValueError):
        return NO_DATA


def page(title, body):
    return ('<!DOCTYPE html><html lang="bg"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)}</title></head>'
            f'<body style="margin:0;padding:0;background:#f2f2f2">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f2f2f2"><tr><td align="center" style="padding:12px 6px">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:960px;background:#ffffff;border:1px solid #d9d9d9">'
            f'<tr><td style="background:{HEAD};padding:14px 16px;{FONT};font-size:20px;font-weight:bold;color:#ffffff">{esc(title)}</td></tr>'
            f'{body}'
            f'<tr><td style="padding:14px 16px;{FONT};font-size:12px;color:#555555;border-top:1px solid #d9d9d9">{esc(DISCLAIMER)}</td></tr>'
            '</table></td></tr></table></body></html>')


def facts(rows):
    """Two-column label/value table, used by both reports."""
    cells = ''.join(f'<tr><td style="{CELL};font-weight:bold;width:34%;background:#f7f7f7">{esc(k)}</td><td style="{CELL}">{esc(v)}</td></tr>' for k, v in rows)
    return f'<tr><td style="padding:12px 16px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #d9d9d9">{cells}</table></td></tr>'


def note(text, color='#7a4b00', background='#fff4d6'):
    return f'<tr><td style="padding:0 16px 10px"><div style="padding:9px 11px;background:{background};{FONT};font-size:13px;color:{color}">{esc(text)}</div></td></tr>'


def offer_rows(items, excluded=True):
    """Offers found via /offers only, blacklist removed, biggest discount first."""
    rows = [(p, d) for _, p, d in items if p.get('source') == 'offers' and not (excluded and d.get('excluded'))]
    return sorted(rows, key=lambda x: (discount(x[0])[0] is None, -(discount(x[0])[0] or 0), x[0].get('name') or ''))


def render_offers(items, meta, max_rows=50):
    """Subject, plain text and HTML for the daily offers report.

    meta: started_at, status, found, processed, skipped, hidden, deals, warnings.
    Returns None when nothing was found: an empty table is never sent.
    """
    rows = offer_rows(items)
    if not rows:
        return None
    shown = rows[:max_rows]
    summary = [('Дата и час на обхода', local_time(meta.get('started_at'))), ('Източник', SOURCE),
               ('Намерени оферти', meta.get('found') if meta.get('found') is not None else NO_DATA),
               ('Успешно обработени', meta.get('processed')), ('Пропуснати артикули', meta.get('skipped', 0)),
               ('Общ статус на обхода', STATUS.get(meta.get('status'), meta.get('status') or NO_DATA))]
    if meta.get('hidden'):
        summary.append(('Скрити по blacklist', meta['hidden']))
    if meta.get('deals') is not None:
        summary.append(('Реално ниски цени спрямо историята', f"{meta['deals']} (пращат се с отделно писмо)" if meta['deals'] else 'Няма днес'))
    head = ''.join(f'<th align="{a}" style="padding:8px;background:{HEAD};color:#ffffff;{FONT};font-size:12px;text-align:{a};white-space:nowrap">{esc(t)}</th>'
                   for t, a in (('№', 'right'), ('Продукт', 'left'), ('Разфасовка', 'left'), ('Стара цена', 'right'), ('Офертна цена', 'right'),
                                ('Отстъпка', 'right'), ('Цена за кг/л', 'right'), ('Наличност', 'left'), ('Връзка', 'left')))
    body_rows = []
    text_rows = []
    # Row colour and alignment go in attributes: every byte repeats per cell, and
    # Gmail clips at about 100 KB. Prices and percentages are right-aligned.
    right = (0, 3, 4, 5, 6)
    for n, (p, _) in enumerate(shown, 1):
        pct, _absolute = discount(p)
        values = [n, p.get('name') or NO_DATA, package(p), money(p.get('regular_price')), money(p.get('price')), percent(pct), unit_price(p),
                  AVAILABILITY.get(p.get('availability'), NO_DATA)]
        cells = ''.join(f'<td align="{"right" if i in right else "left"}" style="{ROW_CELL}{";white-space:nowrap" if i in right else ""}{";font-weight:bold" if i in (4, 5) else ""}">{esc(v)}</td>'
                        for i, v in enumerate(values))
        link = f'<a href="{esc(p["url"])}" style="color:#fff;background:{HEAD};padding:4px 7px;text-decoration:none;white-space:nowrap">Отвори продукта</a>'
        body_rows.append(f'<tr bgcolor="{"#ffffff" if n % 2 else "#f1f6f2"}">{cells}<td style="{ROW_CELL}">{link}</td></tr>')
        text_rows.append(f"{n}. {values[1]} | {values[2]} | стара {values[3]} | офертна {values[4]} | отстъпка {values[5]} | {values[6]} | {values[7]}\n   {p['url']}")
    table = ('<tr><td style="padding:4px 16px 12px"><div style="overflow-x:auto">'
             f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;min-width:720px"><tr>{head}</tr>{"".join(body_rows)}</table></div></td></tr>')
    notes = ''.join(note(w) for w in meta.get('warnings', []))
    if len(rows) > len(shown):
        limit = f'Показани са първите {len(shown)} от {len(rows)} оферти, подредени по отстъпка.'
        notes += note(limit, '#1a1a1a', '#eef3ff')
    markup = page(OFFERS_TITLE, facts(summary) + notes + table)
    text = [OFFERS_TITLE, ''] + [f'{k}: {v}' for k, v in summary] + [''] + list(meta.get('warnings', []))
    if len(rows) > len(shown):
        text.append(limit)
    text += [''] + text_rows + ['', DISCLAIMER]
    return OFFERS_TITLE, '\n'.join(text), markup


def render_empty(meta):
    """No offers: a clear diagnostic, never an empty table."""
    rows = [('Дата и час на обхода', local_time(meta.get('started_at'))), ('Източник', SOURCE), ('Намерени оферти', 0),
            ('Общ статус на обхода', STATUS.get(meta.get('status'), NO_DATA)),
            ('Какво значи', 'Няма оферти, които да могат да бъдат показани. Това е необичайно и вероятно означава промяна в страницата.'),
            ('Следващо действие', 'Отворете https://ebag.bg/offers в браузър. Ако там има оферти, parser-ът трябва да бъде проверен, после: ebag run --send')]
    subject = OFFERS_TITLE + ': няма оферти'
    return subject, '\n'.join([subject, ''] + [f'{k}: {v}' for k, v in rows] + ['', DISCLAIMER]), page(subject, facts(rows))


def render_failure(title, diag, advice, state='Неуспешен обход'):
    """Failure mail: a small facts table, no traceback."""
    status = diag.get('status')
    rows = [('Статус', state), ('Етап', diag.get('stage') or NO_DATA), ('HTTP статус', status if status else NO_DATA),
            ('URL', diag.get('url') or NO_DATA), ('Опити', diag.get('attempts_text') or diag.get('attempts') or NO_DATA),
            ('Последна грешка', diag.get('error') or NO_DATA), ('Време', local_time(diag.get('ts'))),
            ('Временна ли е', diag.get('transient_text') or NO_DATA), ('Следващо действие', advice)]
    if diag.get('run_id'):
        rows.append(('Run ID за логовете', diag['run_id']))
    return '\n'.join([title, ''] + [f'{k}: {v}' for k, v in rows] + ['', DISCLAIMER]), page(title, facts(rows))
