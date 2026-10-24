import argparse,fcntl,json,os,sys,time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from .core import now,prepare,history,evaluate
from .db import Repository
from .crawler import Client
from .notify import SMTP,deliver
from .matching import candidates
from .report import render
from .ebag_source import adapt

from .settings import load as config,load_env

def run(c,db,source=None,send=False,output='data/report.json'):
    repo=Repository(db); run_id=repo.start(); scanned=added=errors=0; items=[]
    try:
        client=None
        if source:raw=json.loads(Path(source).read_text())
        else:
            tracked=[r[0] for r in repo.conn.execute('SELECT external_product_id FROM products WHERE active=1 ORDER BY last_seen_at ASC')]
            client=Client(c);raw=client.collect(tracked)
        if not isinstance(raw,list) or not raw:raise ValueError('Input must contain at least one observation')
        seen=set()
        for record in raw:
            try:
                p=prepare(record); p.setdefault('observed_at',now())
                rule=c.get('storage_rules',{}).get(str(p.get('external_id')), {})
                p.update({k:rule[k] for k in ('storage_verified','shelf_stable') if k in rule})
                dt=datetime.fromisoformat(p['observed_at'])
                if dt.tzinfo is None:raise ValueError('Timezone required')
                if p['identity_hash'] in seen:continue
                seen.add(p['identity_hash'])
                pid,is_new=repo.save(p,run_id); scanned+=1;added+=is_new
                h=history(repo.history(pid,p['unit']),p['observed_at'],c); d=evaluate(p,h,c)
                d['product_matches']=candidates(repo,pid,p,c);items.append((pid,p,d))
            except (KeyError,ValueError,TypeError) as exc:
                errors+=1
        if errors:raise RuntimeError(f'{errors} parser errors: notifications suppressed')
        repo.conn.commit()
        Path(output).parent.mkdir(parents=True,exist_ok=True)
        Path(output).write_text(json.dumps([{'product_id':pid,'product':p,'assessment':d} for pid,p,d in items],ensure_ascii=False,indent=2))
        render(items,str(Path(output).with_suffix('.html')),client.stats if client else None)
        if send and source:raise RuntimeError('Sending email from imported/demo data is disabled')
        emails=deliver(repo,items,c,SMTP()) if send else 0
        repo.finish(run_id,'success',scanned,added,errors,{'emails':emails,'source':'import' if source else 'live','coverage':client.stats if client else None})
        if client:client.save_state()
        print(json.dumps({'run':run_id,'products':scanned,'emails':emails,'report':output}))
    except Exception as exc:
        repo.conn.rollback();repo.finish(run_id,'failed',scanned,added,errors,{'error':str(exc)}); raise
    finally:repo.conn.close()

def main():
    parser=argparse.ArgumentParser(description='eBag Price Intelligence')
    parser.add_argument('command',choices=['init','run','schedule','health','backup','report','smtp-test'])
    parser.add_argument('--config',default='config/preferences.yaml');parser.add_argument('--db',default=os.environ.get('DATABASE_PATH','data/ebag.sqlite3'))
    parser.add_argument('--import-file');parser.add_argument('--send',action='store_true');parser.add_argument('--output',default='data/report.json')
    a=parser.parse_args();load_env();c=config(a.config);
    if a.import_file and a.db=='data/ebag.sqlite3':a.db='data/demo.sqlite3'
    Path(a.db).parent.mkdir(parents=True,exist_ok=True)
    with open(a.db+'.lock','w') as lock:
        try:
            if a.command not in ('health','backup'):fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise SystemExit('Another instance holds database lock')
        if a.command=='init':Repository(a.db).conn.close();print('Database initialized')
        elif a.command=='run':run(c,a.db,a.import_file,a.send,a.output)
        elif a.command=='report':
            repo=Repository(a.db);items=[];seen_external=set()
            for row in repo.conn.execute('SELECT h.* FROM price_history h WHERE h.id=(SELECT x.id FROM price_history x WHERE x.product_id=h.product_id ORDER BY x.observed_at DESC,x.id DESC LIMIT 1) ORDER BY h.observed_at DESC,h.id DESC'):
                raw=json.loads(row['raw_data_json'])
                external=str(raw.get('external_id') or row['product_id'])
                if external in seen_external:continue
                seen_external.add(external)
                # Re-render calculations from original captured source; historical rows remain immutable.
                p=prepare(adapt(raw['source_json'],raw['observed_at']) if raw.get('source_json') else raw);h=history(repo.history(row['product_id'],p['unit']),p['observed_at'],c)
                d=evaluate(p,h,c);d['product_matches']=candidates(repo,row['product_id'],p,c);items.append((row['product_id'],p,d))
            latest=repo.conn.execute("SELECT details_json FROM crawl_runs WHERE status='success' ORDER BY id DESC LIMIT 1").fetchone()
            coverage=(json.loads(latest[0]).get('coverage') or {}) if latest else {}
            Path(a.output).parent.mkdir(parents=True,exist_ok=True)
            Path(a.output).write_text(json.dumps([{'product_id':pid,'product':p,'assessment':d} for pid,p,d in items],ensure_ascii=False,indent=2))
            render(items,str(Path(a.output).with_suffix('.html')),coverage);repo.conn.commit();repo.conn.close();print('HTML report generated')
        elif a.command=='smtp-test':
            if not a.send:raise SystemExit('Use --send to explicitly request a test message')
            for recipient in c['notification_email']:SMTP().send(recipient,'eBag: проверка на email известията','Тестово съобщение от eBag Price Intelligence. Това не е препоръка за покупка.','<p>Тестово съобщение от eBag Price Intelligence.</p>')
            print('SMTP accepted test message; check recipient inbox/spam')
        elif a.command=='backup':
            import sqlite3
            repo=Repository(a.db)
            with sqlite3.connect(a.output) as dst:repo.conn.backup(dst)
            repo.conn.close();print('Backup complete')
        elif a.command=='health':
            repo=Repository(a.db);last=repo.conn.execute("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()
            db_ok=repo.conn.execute('PRAGMA quick_check').fetchone()[0]=='ok'
            good=bool(db_ok and last and last['status']=='success' and last['details_json'] and json.loads(last['details_json']).get('source')=='live' and (datetime.fromisoformat(now())-datetime.fromisoformat(last['finished_at'])).total_seconds()<36*3600)
            smtp_ready=bool(os.environ.get('SMTP_HOST') and os.environ.get('SMTP_FROM'))
            repo.conn.execute('INSERT INTO system_health(checked_at,database_status,email_status,crawler_status,details_json) VALUES(?,?,?,?,?)',(now(),'ok' if db_ok else 'failed','configured_not_verified' if smtp_ready else 'unconfigured','healthy' if good else 'stale_or_failed',json.dumps(dict(last) if last else {})));repo.conn.commit();repo.conn.close()
            print(json.dumps({'database':db_ok,'live_crawler_healthy':good,'smtp_configured':smtp_ready}));sys.exit(0 if good else 1)
        else:
            # Persisted daily slots survive restart; Europe/Sofia handles DST.
            repo=Repository(a.db);repo.conn.execute('CREATE TABLE IF NOT EXISTS scheduled_slots(day TEXT PRIMARY KEY,status TEXT)');repo.conn.commit()
            while True:
                local=datetime.now(ZoneInfo(c['timezone']));day=local.date().isoformat()
                if local.strftime('%H:%M')>=c['daily_crawl_time'] and not repo.conn.execute('SELECT 1 FROM scheduled_slots WHERE day=?',(day,)).fetchone():
                    repo.conn.execute('INSERT INTO scheduled_slots VALUES(?,?)',(day,'started'));repo.conn.commit()
                    try:run(c,a.db,None,a.send,a.output);status='success'
                    except Exception as exc:print(str(exc),file=sys.stderr,flush=True);status='failed'
                    repo.conn.execute('UPDATE scheduled_slots SET status=? WHERE day=?',(status,day));repo.conn.commit()
                time.sleep(30)
if __name__=='__main__':main()
