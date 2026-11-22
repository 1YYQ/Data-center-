"""Bounded synthetic-pilot admission renewal through existing authenticated APIs.
No provider SDK, model import, expiry edits, activation, or business retries.
"""
import argparse, json, os, time, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx
from . import pilot

PURPOSES=('query.answer','document.embed')
MAX_DAILY=24

def now(): return datetime.now(timezone.utc)
def due(test, current):
    return not test.get('valid_until') or datetime.fromisoformat(test['valid_until']) <= current+timedelta(minutes=5)

def cycle(client, config, state, persist, *, active=True, force=False):
    if state.get('pending'): raise RuntimeError('Unresolved renewal intent; read-only reconciliation required')
    if state.get('blocked'): raise RuntimeError('Renewal paused after failure; inspect original receipts')
    if not active and not force: return 'idle'
    selections=client.get('/admin/service-selections');selections.raise_for_status()
    for selection in selections.json()['items']:
        purpose=selection['purpose']
        if purpose not in PURPOSES: continue
        expected=config['service_profiles'][purpose]
        if not selection['enabled'] or selection['revision_id']!=expected['revision_id'] or selection['binding_hash']!=expected['binding_hash']:
            raise RuntimeError(purpose+': disabled or changed admission; not re-enabled')
        detail=client.get('/admin/service-profiles/'+selection['profile_id']);detail.raise_for_status();profile=detail.json()
        if not profile['enabled'] or profile['active_revision_id']!=selection['revision_id']:raise RuntimeError('Profile disabled or changed')
        tests=[t for t in profile['tests'] if t['revision_id']==selection['revision_id']]
        if any(t['state'] not in ('succeeded','failed') for t in tests):raise RuntimeError('Unsettled self-test; no new call')
        test=next(t for t in tests if t['id']==selection['test_run_id'])
        if not force and not due(test,now()):continue
        day=(now()+timedelta(hours=8)).date().isoformat();counter=state.setdefault('counts',{}).setdefault(day,{})
        if counter.get(purpose,0)>=MAX_DAILY:raise RuntimeError('Daily fixed-fixture cap reached')
        last=state.setdefault('last',{}).get(purpose)
        if last and datetime.fromisoformat(last)+timedelta(minutes=54)>now():continue
        def post(path, body, etag=None):
            key='pilot-renew-'+uuid.uuid4().hex
            state['pending']={'path':path,'key':key,'purpose':purpose};persist()
            response=client.post(path,json=body,headers={'Idempotency-Key':key,**({'If-Match':etag} if etag else {})})
            response.raise_for_status();result=response.json()
            state.setdefault('receipts',[]).append({'path':path,'key':key,'result':result});state['pending']=None;persist()
            return result
        # Reserve the bounded attempt durably before the only provider dispatch.
        counter[purpose]=counter.get(purpose,0)+1;state['last'][purpose]=now().isoformat();persist()
        root=f"/admin/service-profiles/{selection['profile_id']}/revisions/{selection['revision_id']}"
        fresh=post(root+'/test-runs',{'fixture_id':'service-synthetic-v1'})
        if fresh['state']!='succeeded':raise RuntimeError('Self-test '+fresh['state']+'; no automatic retry')
        # Refuse to reverse a concurrent safety stop, selection or identity change.
        current=client.get('/admin/service-selections');current.raise_for_status()
        current=next(s for s in current.json()['items'] if s['purpose']==purpose)
        if any(current[k]!=selection[k] for k in ('etag','enabled','revision_id','binding_hash','epoch')):raise RuntimeError('Selection changed during test')
        preview=post('/admin/service-selections/'+purpose+'/previews',{'action':'select','revision_id':selection['revision_id'],'reason':'Bounded SYNTHETIC pilot renewal'},current['etag'])
        if preview['test_run_id']!=fresh['id'] or preview['binding_hash']!=selection['binding_hash']:raise RuntimeError('Preview did not bind exact fresh test')
        selected=post('/admin/service-selections/'+purpose+'/select',{'preview_id':preview['id'],'reason':'Bounded SYNTHETIC pilot renewal'},current['etag'])
        if selected['test_run_id']!=fresh['id']:raise RuntimeError('Fresh test not selected')
    return 'checked'

def active_users(config):
    from .db import build_engine
    from .models import AuthSession
    from sqlalchemy import select
    engine=build_engine(pilot.database_url_for_config())
    try:
        with engine.connect() as conn:
            ids=[config['synthetic_allowlist'][key] for key in ('employee_user_id','knowledge_manager_user_id','admin_user_id')]
            return bool(conn.execute(select(AuthSession.id).where(AuthSession.user_id.in_(ids),AuthSession.revoked_at.is_(None),AuthSession.expires_at>now(),AuthSession.last_seen_at>now()-timedelta(minutes=15)).limit(1)).first())
    finally:engine.dispose()

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--once',action='store_true');parser.add_argument('--verify-renewal',action='store_true');args=parser.parse_args()
    config,path=pilot._read_config();file=path.parent/'renewal-state.json'
    lock=path.parent/'renewal.lock'
    # OS releases the singleton lock on exit; an abandoned file is harmless.
    import msvcrt
    with lock.open('a+b') as handle:
        handle.seek(0);handle.write(b'0');handle.flush();handle.seek(0)
        msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        state=json.loads(file.read_text()) if file.exists() else {}
        def persist():
            state.update(pid=os.getpid(),observed_at=now().isoformat(),model_imported=('torch' in __import__('sys').modules))
            temporary=file.with_suffix('.tmp');temporary.write_text(json.dumps(state,indent=2),encoding='utf-8');os.replace(temporary,file)
        while True:
            try:
                health=httpx.get('http://127.0.0.1:8879/health',trust_env=False,timeout=5);health.raise_for_status()
                active=active_users(config) or args.verify_renewal
                if active:
                    with httpx.Client(base_url='http://127.0.0.1:8879',trust_env=False,timeout=120) as client:
                        cred=pilot.load_credentials(path)['admin'];login=client.post('/auth/login',json={'username':cred['username'],'password':cred['password']});login.raise_for_status()
                        client.headers['X-CSRF-Token']=login.json()['csrf_token'];client.headers['X-Workspace-CSRF']=login.json()['csrf_token']
                        try:state['status']=cycle(client,config,state,persist,force=args.verify_renewal)
                        finally:client.post('/auth/logout')
                else:state['status']='idle'
                persist()
            except Exception as error:
                state['blocked']=True;state['status']='paused';state['safe_error']=type(error).__name__+': '+str(error)[:200];persist();raise
            if args.once:return
            args.verify_renewal=False
            time.sleep(60)

if __name__=='__main__':main()
