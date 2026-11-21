import json
import uuid

from tests.test_system import system, send, claim


def finish(c,bh,d,body='@owner done',**kwargs):
    return c.post('/api/bridge/deliveries/'+d['id']+'/finish',headers=bh,json={'lease':d['lease'],'body':body,**kwargs})


def test_off_switch_blocks_mentions_and_rate_limit_disables_only_failed_session(system):
    c,r,b,bh,ss,store=system;base=f'/api/rooms/{r["id"]}'
    c.post(base+'/members/'+ss[0]['id']+'/control',json={'action':'disable'}).raise_for_status()
    message=send(c,r,'@all begin').json()
    assert next(d for d in message['deliveries'] if d['name']=='claude1')['status']=='skipped'
    assert claim(c,bh,ss[0]) is None
    c.post(base+'/members/'+ss[0]['id']+'/control',json={'action':'enable'}).raise_for_status()
    assert claim(c,bh,ss[0]) is None  # Enabling never replays skipped work.
    send(c,r,'@claude1 first').raise_for_status();d=claim(c,bh,ss[0])
    send(c,r,'@claude1 queued').raise_for_status()
    finish(c,bh,d,body=None,error='HTTP 429 rate_limit_error',error_kind='rate_limit').raise_for_status()
    snap=c.get(base).json();m=next(m for m in snap['members'] if m['id']==ss[0]['id'])
    assert m['paused']==1 and m['disabled_reason']=='rate_limit'
    assert all(not m['paused'] for m in snap['members'] if m['id'] in [s['id'] for s in ss[1:]])
    assert claim(c,bh,ss[0]) is None
    assert send(c,r,'@claude1 no auto restart').json()['deliveries'][0]['status']=='skipped'
    worker={'Authorization':'Bearer '+ss[0]['token']}
    assert send(c,r,'@owner late',worker,cause=d['message']['id']).status_code==403
    c.post(base+'/members/'+ss[0]['id']+'/control',json={'action':'enable'}).raise_for_status()
    assert next(m for m in c.get(base).json()['members'] if m['id']==ss[0]['id'])['disabled_reason']==''


def test_individual_round_budgets_include_queued_work_and_reset_on_new_root(system):
    c,r,b,bh,ss,store=system;base=f'/api/rooms/{r["id"]}'
    limits={ss[0]['id']:1,ss[1]['id']:2,ss[2]['id']:0}
    c.put(base+'/round-limit',json={'max_rounds':8,'round_mode':'individual','session_rounds':limits}).raise_for_status()
    root=send(c,r,'@all begin').json();one=claim(c,bh,ss[0]);two=claim(c,bh,ss[1]);three=claim(c,bh,ss[2])
    assert one['round_budget']['last'] and one['round_budget']['max']==1
    assert two['round_budget']['remaining']==2 and not two['round_budget']['last']
    assert three['round_budget']['max']==0 and three['round_budget']['remaining'] is None
    h={'Authorization':'Bearer '+ss[2]['token']}
    # Multiple peer messages can queue before a target reaches its threshold.
    send(c,r,'@codex1 first queued',h,cause=root['id']).raise_for_status()
    send(c,r,'@codex1 extra queued',h,cause=root['id']).raise_for_status()
    finish(c,bh,two).raise_for_status();second=claim(c,bh,ss[1]);assert second['round_budget']['current']==2 and second['round_budget']['last']
    finish(c,bh,second).raise_for_status();assert claim(c,bh,ss[1]) is None
    assert send(c,r,'@claude1 already used',h,cause=root['id']).json()['deliveries'][0]['status']=='skipped'
    assert send(c,r,'@codex1 already used',h,cause=root['id']).json()['deliveries'][0]['status']=='skipped'
    finish(c,bh,one).raise_for_status();finish(c,bh,three).raise_for_status()
    send(c,r,'@codex1 fresh human task').raise_for_status();fresh=claim(c,bh,ss[1]);assert fresh['round_budget']['current']==1
    # Settings changes do not mutate an existing task's per-session budget.
    c.put(base+'/round-limit',json={'max_rounds':1,'round_mode':'global'}).raise_for_status()
    assert fresh['round_budget']['max']==2


def test_manager_private_history_quotes_routing_and_own_bridge_management(system):
    c,r,b,bh,ss,store=system;base=f'/api/rooms/{r["id"]}'
    manager=c.post('/api/bridge/sessions',headers=bh,json={'name':'manager','harness':'codex','role':'manage own project','session_type':'manager','model_control':True}).json()
    mh={'Authorization':'Bearer '+manager['token']};wh={'Authorization':'Bearer '+ss[0]['token']}
    public=send(c,r,'@all worker task').json();assert 'manager' not in public['targets'] and claim(c,bh,manager) is None
    private=send(c,r,'@manager private-secret').json();d=claim(c,bh,manager)
    assert d['message']['private_to']==manager['id']
    finish(c,bh,d,'@owner private-answer').raise_for_status()
    for headers in (wh,bh):
        history=c.get(base+'/messages',headers=headers).json()
        assert all('private-' not in m['body'] for m in history)
    assert all(m['name']!='manager' for m in c.get(base,headers=wh).json()['members'])
    assert len(c.get(base+'/messages',headers=mh).json())==3
    assert len(c.get(base+'/messages?channel='+manager['id']).json())==2
    assert len(c.get(base+'/messages?channel=').json())==1
    assert c.get(base+'/messages?channel='+manager['id'],headers=wh).status_code==403
    assert send(c,r,'@owner steal quote',wh,reply_to=private['id'],cause=public['id']).status_code==400
    assert send(c,r,'@claude1 accidental private quote',reply_to=private['id']).status_code==403
    assert send(c,r,'@manager @claude1 mixed').status_code==400
    assert send(c,r,'@all private leak',mh,cause=private['id']).status_code==400
    assert send(c,r,'@manager worker trigger',wh,cause=public['id']).status_code==400
    c.post(base+'/members/'+ss[1]['id']+'/control',headers=mh,json={'action':'disable'}).raise_for_status()
    assert c.post(base+'/members/'+ss[1]['id']+'/control',headers=wh,json={'action':'enable'}).status_code==403
    other=c.post('/api/agent/join',headers={'Authorization':'Bearer '+r['join_token']},json={'name':'other-bridge'}).json()
    other_member=c.post('/api/bridge/sessions',headers={'Authorization':'Bearer '+other['token']},json={'name':'outside','role':'other','harness':'command'}).json()
    assert c.post(base+'/members/'+other_member['id']+'/control',headers=mh,json={'action':'disable'}).status_code==403
    assert c.post(base+'/control',headers=mh,json={'action':'rotate-token'}).status_code==403


def test_model_catalog_and_manager_configuration_are_scoped_and_versioned(system):
    c,r,b,bh,ss,store=system;base=f'/api/rooms/{r["id"]}'
    manager=c.post('/api/bridge/sessions',headers=bh,json={'name':'manager','harness':'codex','role':'manage','session_type':'manager','model_control':True}).json()
    mh={'Authorization':'Bearer '+manager['token']}
    c.post('/api/bridge/heartbeat',headers=bh,json={'sessions':[{'id':ss[0]['id'],'model_control':True,'model':'','model_catalog':{'models':[{'id':'model-a','label':'Model A'}],'source':'fixture','updated':123}}]}).raise_for_status()
    settings=next(m for m in c.get(base).json()['members'] if m['id']==ss[0]['id'])['model_settings']
    assert settings['catalog']==[{'id':'model-a','label':'Model A'}]
    result=c.post(base+'/members/'+ss[0]['id']+'/model',headers=mh,json={'model':'model-a','revision':settings['revision'],'reasoning_effort':'high','timeout_seconds':90});result.raise_for_status()
    assert c.post(base+'/members/'+ss[0]['id']+'/model',headers=mh,json={'model':'model-b','revision':settings['revision']}).status_code==409
    configurations=c.post('/api/bridge/heartbeat',headers=bh,json={}).json()['model_settings']
    config=next(x for x in configurations if x['id']==ss[0]['id']);assert config['reasoning_effort']=='high' and config['timeout_seconds']==90
    c.post(base+'/members/'+ss[0]['id']+'/models/refresh',headers=mh,json={}).raise_for_status()
    assert next(x for x in c.post('/api/bridge/heartbeat',headers=bh,json={}).json()['model_settings'] if x['id']==ss[0]['id'])['catalog_refresh']>0
    settings=next(m for m in c.get(base).json()['members'] if m['id']==ss[0]['id'])['model_settings']
    c.post(base+'/members/'+ss[0]['id']+'/model',json={'model':'model-a','revision':settings['revision'],'timeout_seconds':0}).raise_for_status()
    configurations=c.post('/api/bridge/heartbeat',headers=bh,json={}).json()['model_settings']
    assert next(x for x in configurations if x['id']==ss[0]['id'])['timeout_seconds']==0
