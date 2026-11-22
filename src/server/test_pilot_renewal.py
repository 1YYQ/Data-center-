import json
from datetime import timedelta
import httpx, pytest
from . import pilot_renewal as renewal

def setup(monkeypatch, fail=False):
 monkeypatch.setattr(renewal,'PURPOSES',('query.answer',))
 selected={'purpose':'query.answer','profile_id':'p','revision_id':'r','binding_hash':'hash','enabled':True,'active_revision_id':'r','etag':'"1"','epoch':0,'test_run_id':'old'}
 tests=[{'id':'old','revision_id':'r','state':'succeeded','valid_until':(renewal.now()+timedelta(minutes=1)).isoformat()}]
 posts=[]
 def request(req):
  if req.method=='GET':return httpx.Response(200,json={'items':[selected]} if req.url.path.endswith('service-selections') else {**selected,'tests':tests})
  posts.append(req.url.path)
  if fail:raise httpx.ReadTimeout('lost response')
  if req.url.path.endswith('test-runs'):return httpx.Response(202,json={'id':'fresh','state':'succeeded'})
  if req.url.path.endswith('previews'):return httpx.Response(200,json={'id':'preview','test_run_id':'fresh','binding_hash':'hash'})
  return httpx.Response(200,json={'test_run_id':'fresh'})
 return httpx.Client(base_url='http://127.0.0.1:8879',transport=httpx.MockTransport(request)),{'service_profiles':{'query.answer':{'revision_id':'r','binding_hash':'hash'}}},posts,selected

def test_unknown_dispatch_keeps_intent_and_never_reposts(monkeypatch):
 c,config,posts,_=setup(monkeypatch,True);state={};saved=[]
 with pytest.raises(httpx.ReadTimeout):renewal.cycle(c,config,state,lambda:saved.append(json.loads(json.dumps(state))))
 assert state['pending']['key'] and saved[-1]['pending']['key']==state['pending']['key']
 with pytest.raises(RuntimeError,match='Unresolved'):renewal.cycle(c,config,state,lambda:None)
 assert len(posts)==1 and sum(next(iter(state['counts'].values())).values())==1

def test_actual_test_preview_selection_order_and_cooldown(monkeypatch):
 c,config,posts,_=setup(monkeypatch);state={}
 renewal.cycle(c,config,state,lambda:None)
 assert [p.rsplit('/',1)[-1] for p in posts]==['test-runs','previews','select']
 assert not state['pending']
 renewal.cycle(c,config,state,lambda:None,force=True)
 assert len(posts)==3

def test_idle_disable_and_budget_never_dispatch(monkeypatch):
 c,config,posts,selected=setup(monkeypatch)
 assert renewal.cycle(c,config,{},lambda:None,active=False)=='idle'
 selected['enabled']=False
 with pytest.raises(RuntimeError,match='disabled'):renewal.cycle(c,config,{},lambda:None)
 selected['enabled']=True
 day=(renewal.now()+timedelta(hours=8)).date().isoformat()
 with pytest.raises(RuntimeError,match='cap'):renewal.cycle(c,config,{'counts':{day:{'query.answer':24}}},lambda:None)
 assert not posts
