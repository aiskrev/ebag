import argparse,json,os,sys,time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from .core import now,prepare,history,evaluate
from .db import Repository
from .crawler import Client
from .notify import SMTP,deliver,operator_alert,pending,recipients,resolve
from .matching import candidates
from .report import render
from .ebag_source import adapt

from .ops import Busy,exclusive,put_slot,rotate_backup,tick
from .settings import load as config,load_env

def run(c,db,source=None,send=False,output='data/report.json'):
    if send and source:raise RuntimeError('Sending email from imported/demo data is disabled')
    repo=Repository(db); run_id=repo.start(); scanned=added=errors=0; items=[];samples=[]
    try:
        client=None
        if source:raw=json.loads(Path(source).read_text())
        else:
            tracked=[r[0] for r in repo.conn.execute('SELECT external_product_id FROM products WHERE active=1 ORDER BY last_seen_at ASC')]
            client=Client(c);raw=client.collect(tracked)
            errors=client.stats['parse_error_count'];samples=list(client.stats['parse_errors'])
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
                errors+=1;samples.append({'id':record.get('external_id') if isinstance(record,dict) else None,'error':repr(exc)[:200]})
        if not items or (errors>=5 and errors>c['crawl'].get('max_parse_error_ratio',.05)*(errors+scanned)):raise RuntimeError(f'{errors} parser errors in {errors+scanned} records: schema drift')
        # Isolated bad records keep the day's valid observations but suppress email.
        repo.conn.commit()
        Path(output).parent.mkdir(parents=True,exist_ok=True)
        Path(output).write_text(json.dumps([{'product_id':pid,'product':p,'assessment':d} for pid,p,d in items],ensure_ascii=False,indent=2))
        render(items,str(Path(output).with_suffix('.html')),client.stats if client else None)
        if client:client.save_state()
        status='partial' if errors else 'success';emails=0;email_error=None
        if send and not errors:
            # The crawl already succeeded: a mail problem must not erase that fact.
            try:emails=deliver(repo,items,c,SMTP())
            except Exception as exc:email_error=str(exc) or type(exc).__name__
        repo.finish(run_id,status,scanned,added,errors,{'emails':emails,'email_error':email_error,'source':'import' if source else 'live','coverage':client.stats if client else None,'parser_errors':samples[:20]})
        result={'run':run_id,'status':status,'products':scanned,'errors':errors,'emails':emails,'email_error':email_error,'report':output}
        print(json.dumps(result),flush=True)
        return result
    except Exception as exc:
        repo.conn.rollback();repo.finish(run_id,'failed',scanned,added,errors,{'error':str(exc)}); raise
    finally:repo.conn.close()

def health(repo):
    last=repo.conn.execute("SELECT * FROM crawl_runs WHERE status<>'running' ORDER BY id DESC LIMIT 1").fetchone()
    # A crawl in progress is not a failure: judge the newest finished live run.
    good_run=repo.conn.execute("SELECT * FROM crawl_runs WHERE status IN ('success','partial') AND json_extract(details_json,'$.source')='live' ORDER BY id DESC LIMIT 1").fetchone()
    db_ok=repo.conn.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    age=(datetime.fromisoformat(now())-datetime.fromisoformat(good_run['finished_at'])).total_seconds() if good_run else None
    good=bool(db_ok and age is not None and age<36*3600)
    smtp_ready=bool(os.environ.get('SMTP_HOST') and os.environ.get('SMTP_FROM'))
    detail=json.loads(last['details_json'] or '{}') if last else {}
    slot=repo.conn.execute('SELECT day,status,attempts,detail FROM scheduled_slots ORDER BY day DESC LIMIT 1').fetchone()
    report={'database':db_ok,'live_crawler_healthy':good,'smtp_configured':smtp_ready,'last_run_status':last['status'] if last else None,'last_error':detail.get('error'),'last_success_at':good_run['finished_at'] if good_run else None,'last_email_error':detail.get('email_error'),'uncertain_notifications':len(pending(repo)),'latest_slot':dict(slot) if slot else None}
    repo.conn.execute('INSERT INTO system_health(checked_at,last_successful_crawl,database_status,email_status,crawler_status,details_json) VALUES(?,?,?,?,?,?)',(now(),report['last_success_at'],'ok' if db_ok else 'failed','configured_not_verified' if smtp_ready else 'unconfigured','healthy' if good else 'stale_or_failed',json.dumps(report)));repo.conn.commit()
    return report

def schedule(c,db,send,output):
    """Persisted daily slots survive restart; Europe/Sofia handles DST."""
    with exclusive(db+'.scheduler.lock'):
        repo=Repository(db);backup=c.get('backup',{});print(f"Scheduler started: daily {c['daily_crawl_time']} {c['timezone']}",flush=True)
        def execute():
            # The run lock is held only while crawling, so operator commands stay usable.
            with exclusive(db+'.lock'):return run(c,db,None,send,output)
        while True:
            local=datetime.now(ZoneInfo(c['timezone']));day=local.date().isoformat()
            status=tick(repo,c,execute,local,lambda kind,detail:operator_alert(repo,c,SMTP(),day,kind,detail))
            if status in ('success','partial') and backup.get('enabled',True):
                try:print('Backup: '+str(rotate_backup(repo.conn,backup.get('dir','data/backups'),backup.get('keep',7),day)),flush=True)
                except Exception as exc:print(f'Backup failed: {exc}',file=sys.stderr,flush=True)
            time.sleep(30)

def main():
    parser=argparse.ArgumentParser(description='eBag Price Intelligence')
    parser.add_argument('command',choices=['init','run','schedule','health','backup','report','smtp-test','notifications'])
    parser.add_argument('--config',default='config/preferences.yaml');parser.add_argument('--db',default=os.environ.get('DATABASE_PATH','data/ebag.sqlite3'))
    parser.add_argument('--import-file');parser.add_argument('--send',action='store_true');parser.add_argument('--output',default='data/report.json')
    parser.add_argument('--resolve',type=int,help='notification id to reconcile');parser.add_argument('--as',dest='outcome',choices=['sent','failed'],help='verdict after checking SMTP logs')
    a=parser.parse_args();load_env();c=config(a.config)
    if a.import_file and a.db=='data/ebag.sqlite3':a.db='data/demo.sqlite3'
    Path(a.db).parent.mkdir(parents=True,exist_ok=True)
    try:dispatch(a,c)
    except Busy as exc:raise SystemExit(str(exc))

def dispatch(a,c):
    if a.command=='init':Repository(a.db).conn.close();print('Database initialized')
    elif a.command=='run':
        with exclusive(a.db+'.lock'):result=run(c,a.db,a.import_file,a.send,a.output)
        if a.send and not a.import_file:
            # An operator's recovery run satisfies today's slot, so the scheduler does not repeat it.
            repo=Repository(a.db);put_slot(repo,datetime.now(ZoneInfo(c['timezone'])).date().isoformat(),result['status'],detail='manual run');repo.conn.close()
        if result['status']!='success' or result['email_error']:sys.exit(2)
    elif a.command=='report':
        repo=Repository(a.db);items=[];seen_external=set()
        for row in repo.conn.execute('SELECT h.* FROM price_history h WHERE h.id=(SELECT x.id FROM price_history x WHERE x.product_id=h.product_id ORDER BY x.observed_at DESC,x.id DESC LIMIT 1) ORDER BY h.observed_at DESC,h.id DESC').fetchall():
            raw=json.loads(row['raw_data_json'])
            external=str(raw.get('external_id') or row['product_id'])
            if external in seen_external:continue
            seen_external.add(external)
            # Re-render calculations from original captured source; historical rows remain immutable.
            p=prepare(adapt(raw['source_json'],raw['observed_at']) if raw.get('source_json') else raw);h=history(repo.history(row['product_id'],p['unit']),p['observed_at'],c)
            d=evaluate(p,h,c);d['product_matches']=candidates(repo,row['product_id'],p,c);items.append((row['product_id'],p,d))
        latest=repo.conn.execute("SELECT details_json FROM crawl_runs WHERE status IN ('success','partial') ORDER BY id DESC LIMIT 1").fetchone()
        coverage=(json.loads(latest[0]).get('coverage') or {}) if latest else {}
        Path(a.output).parent.mkdir(parents=True,exist_ok=True)
        Path(a.output).write_text(json.dumps([{'product_id':pid,'product':p,'assessment':d} for pid,p,d in items],ensure_ascii=False,indent=2))
        render(items,str(Path(a.output).with_suffix('.html')),coverage);repo.conn.commit();repo.conn.close();print('HTML report generated')
    elif a.command=='smtp-test':
        if not a.send:raise SystemExit('Use --send to explicitly request a test message')
        try:
            for recipient in recipients(c):SMTP().send(recipient,'eBag: проверка на email известията','Тестово съобщение от eBag Price Intelligence. Това не е препоръка за покупка.','<p>Тестово съобщение от eBag Price Intelligence.</p>')
        except Exception as exc:raise SystemExit(f'SMTP test failed: {exc}')
        print('SMTP accepted test message; check recipient inbox/spam')
    elif a.command=='notifications':
        repo=Repository(a.db)
        if a.resolve is not None:
            if not a.outcome:raise SystemExit('Use --as sent or --as failed')
            changed=resolve(repo,a.resolve,a.outcome);repo.conn.close()
            if not changed:raise SystemExit('No pending notification with this id')
            print('Notification resolved')
        else:print(json.dumps(pending(repo),ensure_ascii=False,indent=2));repo.conn.close()
    elif a.command=='backup':
        import sqlite3
        repo=Repository(a.db);dst=sqlite3.connect(a.output)
        try:repo.conn.backup(dst)
        finally:dst.close();repo.conn.close()
        print('Backup complete')
    elif a.command=='health':
        repo=Repository(a.db);report=health(repo);repo.conn.close()
        print(json.dumps(report,ensure_ascii=False));sys.exit(0 if report['live_crawler_healthy'] else 1)
    else:schedule(c,a.db,a.send,a.output)
if __name__=='__main__':main()
