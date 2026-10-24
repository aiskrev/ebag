"""Self-contained human-readable report, no external assets or analytics."""
import html
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

def render(items,path,coverage=None):
    esc=html.escape
    rows=[]
    for pid,p,d in sorted(items,key=lambda x:(x[2]['excluded'],-x[2]['score'])):
        if d['excluded']:continue
        url=p.get('url') or '';parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.netloc!='ebag.bg':url=''
        real='няма история' if d['real_discount'] is None else f"{d['real_discount']:.1f}%"
        up='—' if p.get('normalized_price') is None else f"{p['normalized_price']:.2f} €/{p['unit']}"
        hist=d['history'];flags=' · '.join(d['flags'])
        rows.append(f'''<tr data-label="{esc(d['classification'])}" data-watch="{int(d['watchlist'])}"><td><a href="{esc(url,quote=True)}" rel="noreferrer">{esc(p['name'])}</a><small>{esc(str(p.get('package') or ''))} · {esc(str(p.get('category') or ''))}</small></td><td>{p['price']:.2f} €<small>{esc(up)}</small></td><td>{esc(real)}<small>{hist['observations']} предишни дневни наблюдения</small></td><td>{d['score']:.0f}<small>{esc(d['classification'])}</small></td><td>{esc(d['confidence'])}<small>{esc(flags)}</small></td><td>{esc(d['quantity'])}<small>{esc(str(p.get('expiration_date') or 'неизвестен срок'))}</small></td></tr>''')
    coverage=coverage or {}
    text=f'''<!doctype html><html lang="bg"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>eBag · Price Intelligence</title>
<style>body{{font:15px system-ui,sans-serif;color:#deece8;background:#091713;margin:0;padding:32px}}main{{max-width:1400px;margin:auto}}header{{border-bottom:1px solid #345447;padding-bottom:20px}}h1{{font-size:34px;letter-spacing:-1px}}em{{color:#8bebb9;font-style:normal}}.notice{{background:#19382b;border:1px solid #325d47;padding:18px;border-radius:12px;line-height:1.65}}.controls{{display:flex;gap:12px;margin:24px 0;flex-wrap:wrap}}input,select{{background:#142b21;color:#e6fff2;border:1px solid #4a7160;border-radius:8px;padding:12px;font:inherit}}input{{flex:1;min-width:200px}}.wrap{{overflow:auto}}table{{border-collapse:collapse;width:100%}}th,td{{padding:16px 12px;text-align:left;border-bottom:1px solid #264236;vertical-align:top}}th{{color:#99b8a9;font-size:12px;text-transform:uppercase}}small{{display:block;margin-top:6px;color:#9cb6aa;font-size:12px;max-width:300px}}a{{color:#e4fff1;text-decoration:none}}a:hover{{color:#8bebb9;text-decoration:underline}}footer{{color:#9cb6aa;margin-top:24px}}[hidden]{{display:none}}@media(max-width:600px){{body{{padding:16px}}h1{{font-size:26px}}}}</style>
<main><header><p><em>EBAG / PRICE INTELLIGENCE</em></p><h1>Цената има история.</h1><p>Последно наблюдение: {esc(max((p['observed_at'] for _,p,_ in items),default='—'))}</p></header>
<p class="notice">Това са реално наблюдавани цени, когато отчетът е създаден чрез live run. Примерните импорти са отделни. При кратка история няма доказана добра сделка и не се изпраща email само заради зачеркната цена. Blacklist продуктите са скрити.<br>Обходени страници: {coverage.get('listing_pages','—')} · Допълнителни страници остават: {esc(str(coverage.get('listing_truncated','—')))} · Стари продукти за следващо обновяване: {coverage.get('tracked_pending','—')}</p>
<div class="controls"><input id="q" type="search" aria-label="Търси продукт" placeholder="Търси продукт или категория…"><select id="filter" aria-label="Филтър"><option value="all">Всички</option><option value="buy">BUY / STRONG BUY</option><option value="watch">Watchlist</option></select></div><div class="wrap"><table><thead><tr><th>Продукт</th><th>Текуща цена</th><th>Историческо намаление</th><th>Оценка / 100</th><th>Увереност</th><th>Количество / срок</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div><footer>{len(rows)} продукта след blacklist. Без автоматични покупки. Цените и наличностите могат да се променят.</footer></main>
<script>const q=document.querySelector('#q'),f=document.querySelector('#filter');function apply(){{for(const r of document.querySelectorAll('tbody tr')){{let ok=r.textContent.toLocaleLowerCase('bg').includes(q.value.toLocaleLowerCase('bg'));if(f.value==='buy')ok=ok&&['BUY','STRONG BUY'].includes(r.dataset.label);if(f.value==='watch')ok=ok&&r.dataset.watch==='1';r.hidden=!ok}}}}q.addEventListener('input',apply);f.addEventListener('change',apply);</script></html>'''
    Path(path).write_text(text,encoding='utf-8')
