import hashlib,html,json,os,smtplib,ssl,sys
from datetime import datetime,timedelta
from email.message import EmailMessage
from typing import Protocol
from .core import now,watch
from .emails import FAILURE_TITLE,render_failure
class NotSent(RuntimeError):
    """Failed before the message left us, so it is safe to try again on a later run."""
class Provider(Protocol):
    def send(self,recipient:str,subject:str,text:str,markup:str)->None:...
class SMTP:
    def validate(self):
        if not os.environ.get('SMTP_HOST') or not os.environ.get('SMTP_FROM'):raise RuntimeError('Configure SMTP_HOST and SMTP_FROM in .env')
        if os.environ.get('SMTP_USE_TLS','true').lower()!='true':raise RuntimeError('TLS must be enabled')
    def send(self,recipient,subject,text,markup):
        host=os.environ.get('SMTP_HOST'); sender=os.environ.get('SMTP_FROM')
        if not host or not sender: raise RuntimeError('SMTP_HOST and SMTP_FROM required')
        if os.environ.get('SMTP_USE_TLS','true').lower()!='true':raise RuntimeError('TLS must be enabled')
        msg=EmailMessage(); msg['From']=sender; msg['To']=recipient; msg['Subject']=subject; msg.set_content(text); msg.add_alternative(markup,subtype='html')
        port=int(os.environ.get('SMTP_PORT','587'))
        # SMTP_SSL does not verify the server unless it is given this context.
        context=ssl.create_default_context()
        try:s=smtplib.SMTP_SSL(host,port,timeout=30,context=context) if port==465 else smtplib.SMTP(host,port,timeout=30)
        except (OSError,smtplib.SMTPException) as exc:raise NotSent(f'SMTP connect failed: {exc}') from exc
        with s:
            try:
                if port!=465:s.starttls(context=context)
                if os.environ.get('SMTP_USERNAME'):s.login(os.environ['SMTP_USERNAME'],os.environ.get('SMTP_PASSWORD',''))
            except (OSError,smtplib.SMTPException) as exc:raise NotSent(f'SMTP TLS/login failed: {exc}') from exc
            s.send_message(msg)
def recipients(c):
    return [x.strip() for x in (os.environ.get('NOTIFICATION_EMAIL') or ','.join(c['notification_email'])).split(',') if x.strip()]
ALERTS={
 'review_required':('eBag агент: Спрян е live режимът, нужен е преглед','Спрян (чака преглед)'),
 'access_denied':(FAILURE_TITLE,'Неуспешен обход'),
 'policy_stop':(FAILURE_TITLE,'Неуспешен обход'),
 'crawl_failed':(FAILURE_TITLE,'Неуспешен обход'),
 'parser_errors':('eBag агент: Обход с грешки при разчитане','Частично успешен обход'),
}
RUN='ebag run --send'
def advice(kind,diag):
    """A concrete next step: what failed, whether a retry helps, which command to run."""
    status=diag.get('status');stage=diag.get('stage') or ''
    if kind=='review_required':return 'Нов release или изтекъл 30-дневен преглед. Прегледайте robots, условията и отговорите на https://ebag.bg/offers, обновете crawl.policy_reviewed_at и crawl.reviewed_store_commit в config/preferences.yaml и рестартирайте контейнера; днешният обход тръгва сам.'
    if kind=='access_denied':
        wait=f" Сайтът поиска изчакване (Retry-After: {diag['retry_after']})." if diag.get('retry_after') else ''
        return f'Сайтът ограничи или отказа достъп (HTTP {status or "challenge"}).{wait} Агентът не заобикаля ограничението и опитва отново утре. Ако се повтаря, увеличете crawl.interval_seconds.'
    if kind=='parser_errors':return 'Валидните оферти са записани, но писма с оферти днес не се пращат. Ако се повтаря, схемата на отговора се е променила и parser-ът трябва да се провери.'
    if status==404 and diag.get('kind')=='offers_page':
        return f'Адресът, от който страницата https://ebag.bg/offers зарежда офертите, върна 404. Повторен опит без проверка няма смисъл. Отворете /offers в браузър, проверете заявката към офертите и след поправка изпълнете: {RUN}'
    if diag.get('transient'):
        return f'Временна грешка на сайта или мрежата. Повторният опит има смисъл: агентът опитва отново утре в 08:00. За днешните данни изпълнете: {RUN}'
    return f'Грешката не е временна и ще се повтори без поправка. Етап: {stage or "неизвестен"}. След поправка изпълнете: {RUN}'
def operator_alert(repo,c,provider,day,kind,detail):
    """Best effort, at most once per kind per day. The slot outcome is already saved."""
    title,state=ALERTS[kind];sent=0
    diag=dict(detail) if isinstance(detail,dict) else {'error':str(detail)}
    diag.setdefault('ts',now())
    diag['transient_text']='Да, повторният опит има смисъл' if diag.get('transient') else 'Не, без поправка ще се повтори'
    text,markup=render_failure(title,diag,advice(kind,diag),state)
    try:
        if hasattr(provider,'validate'):provider.validate()
    except Exception as exc:
        print(f'Operator alert not sent ({kind}): {exc}',file=sys.stderr,flush=True);return 0
    for recipient in recipients(c):
        fp=hashlib.sha256(json.dumps(['alert',day,kind]).encode()).hexdigest()
        if repo.conn.execute('SELECT 1 FROM notifications WHERE recipient=? AND message_hash=?',(recipient,fp)).fetchone():continue
        try:provider.send(recipient,title,text,markup)
        except Exception as exc:
            print(f'Operator alert failed ({kind}): {exc}',file=sys.stderr,flush=True);continue
        repo.conn.execute('INSERT INTO notifications(sent_at,notification_type,recipient,message_hash,status) VALUES(?,?,?,?,?)',(now(),'OPERATOR_ALERT',recipient,fp,'sent'));repo.conn.commit();sent+=1
    return sent
def pending(repo):
    return [dict(r) for r in repo.conn.execute("SELECT n.id,n.sent_at,n.notification_type,n.recipient,n.status,n.price_at_notification,p.original_name FROM notifications n LEFT JOIN products p ON p.id=n.product_id WHERE n.status IN ('sending','uncertain') ORDER BY n.id")]
def resolve(repo,notification_id,outcome):
    """Operator verdict after checking SMTP logs. 'failed' allows the offer to be sent again."""
    if outcome not in ('sent','failed'):raise ValueError('Outcome must be sent or failed')
    changed=repo.conn.execute("UPDATE notifications SET status=? WHERE id=? AND status IN ('sending','uncertain')",(outcome,notification_id)).rowcount
    repo.conn.commit();return changed
def eligible(p,d,c):
    w=watch(p,c)
    threshold=(w or {}).get('minimum_real_discount_for_email',c['deal_thresholds']['minimum_real_discount_for_email'])
    return not d['excluded'] and d['real_discount'] is not None and d['real_discount']>=threshold and (d['classification'] in ('STRONG BUY','BUY') or (d['classification']=='WATCH' and d['watchlist'] and d['confidence']!='LOW' and not d['flags'] and d['real_discount']>=threshold))

def deliver(repo,items,c,provider):
    sent=0
    for recipient in recipients(c):
        selected=[]
        for pid,p,d in items:
            if not eligible(p,d,c):continue
            fp=hashlib.sha256(json.dumps([pid,round(p['price'],4),p.get('promotion_start'),p.get('promotion_end')]).encode()).hexdigest()
            old=repo.conn.execute("SELECT * FROM notifications WHERE product_id=? AND recipient=? AND status IN ('sent','sending','uncertain') ORDER BY id DESC LIMIT 1",(pid,recipient)).fetchone()
            # Ambiguous SMTP outcome never auto-retried; operator must reconcile.
            if old and old['status']!='sent':continue
            if repo.conn.execute("SELECT 1 FROM notifications WHERE recipient=? AND message_hash=? AND status<>'failed'",(recipient,fp)).fetchone():continue
            if old and datetime.fromisoformat(p['observed_at'])-datetime.fromisoformat(old['sent_at'])<timedelta(days=c['deal_thresholds']['cooldown_days']):
                drop=(old['price_at_notification']-p['price'])/old['price_at_notification']*100
                different_promo=False
                # A changed promotion period creates a distinct fingerprint, but only
                # a real price/score improvement can bypass cooldown by default.
                minimum=d.get('history',{}).get('minimum')
                new_low=bool(minimum and p['normalized_price']<minimum and p['price']<old['price_at_notification'])
                if drop<c['deal_thresholds']['resend_drop_percent'] and d['score']-old['deal_score']<c['deal_thresholds']['resend_score_increase'] and not new_low:continue
            selected.append((pid,p,d,fp))
        if not selected:continue
        # Credentials/config failures happen before reserving any notifications.
        if hasattr(provider,'validate'):provider.validate()
        lines=['eBag — исторически добри цени (собствени наблюдения)']
        for pid,p,d,fp in selected:
            lines.append(f"{p['name']}: {p['price']:.2f} EUR; {p['normalized_price']:.2f} EUR/{p['unit']}; историческо намаление {d['real_discount']:.1f}%; {d['classification']}; {d['score']}/100; увереност {d['confidence']}; {d['quantity']}. {p.get('url','')}")
            repo.conn.execute("INSERT INTO notifications(product_id,sent_at,notification_type,price_at_notification,deal_score,recipient,message_hash,status) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(recipient,message_hash) DO UPDATE SET sent_at=excluded.sent_at,deal_score=excluded.deal_score,status='sending'",(pid,p['observed_at'],d['classification'],p['price'],d['score'],recipient,fp,'sending'))
        repo.conn.commit()
        try:provider.send(recipient,'eBag: добри цени според собствената история','\n\n'.join(lines),'<html><body>'+''.join('<p>'+html.escape(x)+'</p>' for x in lines)+'</body></html>')
        except Exception as exc:
            status='failed' if isinstance(exc,NotSent) else 'uncertain'
            for *_,fp in selected:repo.conn.execute("UPDATE notifications SET status=? WHERE recipient=? AND message_hash=?",(status,recipient,fp))
            repo.conn.commit();raise
        for *_,fp in selected:repo.conn.execute("UPDATE notifications SET status='sent' WHERE recipient=? AND message_hash=?",(recipient,fp))
        repo.conn.commit();sent+=1
    return sent
