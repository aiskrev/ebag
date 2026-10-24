import hashlib,html,json,os,smtplib,ssl
from datetime import datetime,timedelta
from email.message import EmailMessage
from typing import Protocol
from .core import watch
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
def eligible(p,d,c):
    w=watch(p,c)
    threshold=(w or {}).get('minimum_real_discount_for_email',c['deal_thresholds']['minimum_real_discount_for_email'])
    return not d['excluded'] and d['real_discount'] is not None and d['real_discount']>=threshold and (d['classification'] in ('STRONG BUY','BUY') or (d['classification']=='WATCH' and d['watchlist'] and d['confidence']!='LOW' and not d['flags'] and d['real_discount']>=threshold))

def deliver(repo,items,c,provider):
    sent=0
    for recipient in filter(None,os.environ.get('NOTIFICATION_EMAIL',','.join(c['notification_email'])).split(',')):
        selected=[]
        for pid,p,d in items:
            if not eligible(p,d,c):continue
            fp=hashlib.sha256(json.dumps([pid,round(p['price'],4),p.get('promotion_start'),p.get('promotion_end')]).encode()).hexdigest()
            old=repo.conn.execute("SELECT * FROM notifications WHERE product_id=? AND recipient=? AND status IN ('sent','sending','uncertain') ORDER BY id DESC LIMIT 1",(pid,recipient)).fetchone()
            # Ambiguous SMTP outcome never auto-retried; operator must reconcile.
            if old and old['status']!='sent':continue
            if repo.conn.execute('SELECT 1 FROM notifications WHERE recipient=? AND message_hash=?',(recipient,fp)).fetchone():continue
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
            repo.conn.execute('INSERT INTO notifications(product_id,sent_at,notification_type,price_at_notification,deal_score,recipient,message_hash,status) VALUES(?,?,?,?,?,?,?,?)',(pid,p['observed_at'],d['classification'],p['price'],d['score'],recipient,fp,'sending'))
        repo.conn.commit()
        try:provider.send(recipient,'eBag: добри цени според собствената история','\n\n'.join(lines),'<html><body>'+''.join('<p>'+html.escape(x)+'</p>' for x in lines)+'</body></html>')
        except Exception:
            for *_,fp in selected:repo.conn.execute("UPDATE notifications SET status='uncertain' WHERE recipient=? AND message_hash=?",(recipient,fp))
            repo.conn.commit();raise
        for *_,fp in selected:repo.conn.execute("UPDATE notifications SET status='sent' WHERE recipient=? AND message_hash=?",(recipient,fp))
        repo.conn.commit();sent+=1
    return sent
