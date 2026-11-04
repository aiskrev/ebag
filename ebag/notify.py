import hashlib,html,json,os,smtplib,ssl,sys
from datetime import datetime,timedelta
from email.message import EmailMessage
from typing import Protocol
from .core import now,watch
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
        cls=smtplib.SMTP_SSL if port==465 else smtplib.SMTP
        with cls(host,port,timeout=30) as s:
            if port!=465:s.starttls(context=ssl.create_default_context())
            if os.environ.get('SMTP_USERNAME'):s.login(os.environ['SMTP_USERNAME'],os.environ.get('SMTP_PASSWORD',''))
            s.send_message(msg)
def recipients(c):
    return [x.strip() for x in (os.environ.get('NOTIFICATION_EMAIL') or ','.join(c['notification_email'])).split(',') if x.strip()]
ALERTS={
 'review_required':('Спрян е live режимът: нужен е преглед','eBag е пуснал нов release или прегледът е изтекъл. Прегледайте условията и parser-а, обновете config/preferences.yaml и рестартирайте контейнера.'),
 'access_denied':('Магазинът отказа достъп','Получен е 401/403/429 или challenge. Агентът спря и няма да опитва отново днес. Не се заобикаля; проверете дали достъпът трябва да продължи.'),
 'policy_stop':('Обходът е спрян от защитна проверка','Robots, schema drift или друга защита спря обхода. Нужна е проверка, преди да продължи.'),
 'crawl_failed':('Дневният обход не успя','Всички опити за днес са изчерпани. Проверете логовете и изпълнете ebag run --send след поправка.'),
 'parser_errors':('Обход с parser грешки','Валидните наблюдения са запазени, но email-ите за сделки са спрени за днес. Проверете дали схемата се е променила.'),
}
def operator_alert(repo,c,provider,day,kind,detail):
    """Best effort, at most once per kind per day. The slot outcome is already saved."""
    title,advice=ALERTS[kind];sent=0
    try:
        if hasattr(provider,'validate'):provider.validate()
    except Exception as exc:
        print(f'Operator alert not sent ({kind}): {exc}',file=sys.stderr,flush=True);return 0
    for recipient in recipients(c):
        fp=hashlib.sha256(json.dumps(['alert',day,kind]).encode()).hexdigest()
        if repo.conn.execute('SELECT 1 FROM notifications WHERE recipient=? AND message_hash=?',(recipient,fp)).fetchone():continue
        lines=[advice,'Детайл: '+str(detail),'Това е служебно съобщение, не препоръка за покупка.']
        try:provider.send(recipient,'eBag агент: '+title,'\n\n'.join(lines),'<html><body>'+''.join('<p>'+html.escape(x)+'</p>' for x in lines)+'</body></html>')
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
        except Exception:
            for *_,fp in selected:repo.conn.execute("UPDATE notifications SET status='uncertain' WHERE recipient=? AND message_hash=?",(recipient,fp))
            repo.conn.commit();raise
        for *_,fp in selected:repo.conn.execute("UPDATE notifications SET status='sent' WHERE recipient=? AND message_hash=?",(recipient,fp))
        repo.conn.commit();sent+=1
    return sent
