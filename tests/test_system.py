import concurrent.futures
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.core import Problem, recipients


@pytest.fixture
def system(tmp_path):
    app=create_app(tmp_path)
    client=TestClient(app)
    result=client.post('/api/bootstrap',json={'username':'owner','password':'a-long-test-password','setup_code':(tmp_path/'bootstrap-code').read_text()})
    assert result.status_code==200,result.text
    room=client.post('/api/rooms',json={'name':'协作测试'}).json()
    bridge=client.post('/api/agent/join',headers={'Authorization':'Bearer '+room['join_token']},json={'name':'setup-test'}).json()
    bh={'Authorization':'Bearer '+bridge['token']}
    sessions=[]
    for name in ('claude1','codex1','codex2'):
        response=client.post('/api/bridge/sessions',headers=bh,json={'name':name,'role':'仅完成分派的测试','harness':'command'})
        assert response.status_code==200,response.text
        sessions.append(response.json())
    return client,room,bridge,bh,sessions,app.state.store


def send(client,room,text,headers=None,**kwargs):
    return client.post(f'/api/rooms/{room["id"]}/messages',headers=headers or {},json={'body':text,'nonce':str(uuid.uuid4()),**kwargs})


def claim(client,bh,s):
    return client.post(f'/api/bridge/sessions/{s["id"]}/claim',headers=bh,json={}).json()['delivery']


def test_prefix_mentions_only():
    assert recipients('@claude1，你工作做完了告诉@claude2')==['claude1']
    assert recipients('@claude1 @codex1，处理。后面 @codex2 只是指代')==['claude1','codex1']
    assert recipients('@所有人 开始')==['所有人']
    assert recipients('@claude1\n@codex1 是引用')==['claude1']
    assert recipients('@小克，你工作做完了告诉@小龙')==['小克']
    assert recipients('@吉米奈 @小龙，请处理。后面 @小克 只是指代')==['吉米奈','小龙']
    assert recipients('@小龙。开始')==['小龙']
    with pytest.raises(Problem):recipients('请 @claude1 工作')


def test_dormant_sessions_only_awaken_by_owner(system):
    c,r,b,bh,ss,store=system
    assert claim(c,bh,ss[0]) is None
    result=send(c,r,'@claude1，完成后告诉@codex1')
    assert result.status_code==200,result.text
    assert len(result.json()['deliveries'])==1
    d=claim(c,bh,ss[0]);assert d
    peer=send(c,r,'@codex1 请审查',{'Authorization':'Bearer '+ss[0]['token']},cause=d['message']['id'])
    assert peer.status_code==200,peer.text
    assert peer.json()['deliveries'][0]['status']=='skipped'
    assert claim(c,bh,ss[1]) is None
    send(c,r,'@codex1 现在开始')
    d2=claim(c,bh,ss[1]);assert d2['message']['body']=='@codex1 现在开始'


def test_all_activates_and_quote_does_not_route(system):
    c,r,b,bh,ss,store=system
    first=send(c,r,'@admin 引用里的 @codex2 不触发').json()
    message=send(c,r,'@claude1 请看引用',reply_to=first['id']).json()
    assert message['quote']['name']=='owner'
    assert message['quote']['body']==first['body']
    assert len(message['deliveries'])==1
    broadcast=send(c,r,'@all 请分别开始').json()
    assert len(broadcast['deliveries'])==3
    assert all(d['status']=='pending' for d in broadcast['deliveries'])


def test_auth_and_identity_scoping(system):
    c,r,b,bh,ss,store=system
    no_auth=TestClient(c.app)
    assert no_auth.get('/api/rooms/'+r['id']).status_code==401
    assert c.post('/api/rooms',headers=bh,json={'name':'bad'}).status_code==403
    assert c.post('/api/bridge/sessions',headers={'Authorization':'Bearer '+ss[0]['token']},json={'name':'evil','role':'x','harness':'codex'}).status_code==403
    other=c.post('/api/rooms',json={'name':'other'}).json()
    assert c.get('/api/rooms/'+other['id'],headers=bh).status_code==403
    assert c.post('/api/rooms/'+r['id']+'/control',headers=bh,json={'action':'rotate-token'}).status_code==403
    assert send(c,r,'@owner 未激活消息',{'Authorization':'Bearer '+ss[1]['token']}).status_code==403


def test_cross_room_quote_and_unknown_target_rejected(system):
    c,r,b,bh,ss,store=system
    another=c.post('/api/rooms',json={'name':'another'}).json()
    ref=send(c,another,'@admin hello').json()
    assert send(c,r,'@claude1 see',reply_to=ref['id']).status_code==400
    assert send(c,r,'@notfound hello').status_code==400
    assert send(c,r,'没有收件人').status_code==400


def test_message_idempotency(system):
    c,r,b,bh,ss,store=system
    msg={'body':'@claude1 do it','nonce':'same'}
    a=c.post(f'/api/rooms/{r["id"]}/messages',json=msg).json()
    z=c.post(f'/api/rooms/{r["id"]}/messages',json=msg).json()
    assert a['id']==z['id']
    assert c.post(f'/api/rooms/{r["id"]}/messages',json={**msg,'body':'@claude1 different'}).status_code==409
    claim(c,bh,ss[0])
    scoped={'Authorization':'Bearer '+ss[0]['token']}
    reply={'body':'@admin done','nonce':'inferred-cause'}
    first=c.post(f'/api/rooms/{r["id"]}/messages',headers=scoped,json=reply)
    second=c.post(f'/api/rooms/{r["id"]}/messages',headers=scoped,json=reply)
    assert first.status_code==second.status_code==200
    assert first.json()['id']==second.json()['id']


def test_leases_concurrency_and_atomic_finish(system):
    c,r,b,bh,ss,store=system
    send(c,r,'@claude1 task');actor=store.identity(b['token'])
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        claims=list(pool.map(lambda _:store.claim(actor,ss[0]['id']),range(5)))
    assert sum(d is not None for d in claims)==1
    d=next(d for d in claims if d)
    beat=c.post('/api/bridge/heartbeat',headers=bh,json={'leases':[{'id':d['id'],'lease':d['lease']}]}).json()
    assert beat['lease_seconds']==d['lease_seconds']==90 and beat['valid_leases']==[d['id']] and beat['invalid_leases']==[]
    payload={'lease':d['lease'],'body':'@admin 已完成','native_id':'session-A'}
    path=f'/api/bridge/deliveries/{d["id"]}/finish'
    assert c.post(path,headers=bh,json={**payload,'lease':'wrong'}).status_code==403
    assert c.post(path,headers=bh,json=payload).status_code==200
    assert c.post(path,headers=bh,json=payload).status_code==200
    history=c.get(f'/api/rooms/{r["id"]}/messages').json()
    assert len(history)==2
    assert history[0]['deliveries'][0]['status']=='done'


def test_expired_work_never_auto_replayed(system):
    c,r,b,bh,ss,store=system
    send(c,r,'@claude1 edit files');d=claim(c,bh,ss[0])
    with store.tx() as db:db.execute('UPDATE deliveries SET lease_until=? WHERE id=?',(time.time()-1,d['id']))
    beat=c.post('/api/bridge/heartbeat',headers=bh,json={'leases':[{'id':d['id'],'lease':d['lease']}]}).json()
    assert not beat['valid_leases'] and beat['invalid_leases'][0]['kind']=='lease_expired'
    assert claim(c,bh,ss[0]) is None
    history=c.get(f'/api/rooms/{r["id"]}/messages').json()
    assert history[0]['deliveries'][0]['status']=='failed'
    assert c.post(f'/api/rooms/{r["id"]}/deliveries/{d["id"]}/retry',json={}).status_code==200
    retried=claim(c,bh,ss[0]);assert retried and retried['lease']!=d['lease']


def test_pause_cancels_and_revoke_is_transitive(system):
    c,r,b,bh,ss,store=system
    send(c,r,'@all start');d=claim(c,bh,ss[0])
    c.post(f'/api/rooms/{r["id"]}/control',json={'action':'pause'})
    heartbeat=c.post('/api/bridge/heartbeat',headers=bh,json={'leases':[{'id':d['id'],'lease':d['lease']}]}).json()
    assert heartbeat['valid_leases']==[]
    assert heartbeat['invalid_leases'][0]['kind']=='task_cancelled' and '暂停' in heartbeat['invalid_leases'][0]['reason']
    assert claim(c,bh,ss[1]) is None
    c.post(f'/api/rooms/{r["id"]}/control',json={'action':'resume'})
    assert claim(c,bh,ss[1]) is None
    c.post(f'/api/rooms/{r["id"]}/members/{b["member_id"]}/control',json={'action':'revoke'})
    assert c.get('/api/rooms/'+r['id'],headers=bh).status_code==401
    assert c.get('/api/rooms/'+r['id'],headers={'Authorization':'Bearer '+ss[0]['token']}).status_code==401


def test_rotation_and_session_token_survival(system):
    c,r,b,bh,ss,store=system
    rotated=c.post(f'/api/rooms/{r["id"]}/control',json={'action':'rotate-token'}).json()
    assert c.post('/api/agent/join',headers={'Authorization':'Bearer '+r['join_token']},json={'name':'other'}).status_code==401
    assert c.post('/api/agent/join',headers={'Authorization':'Bearer '+rotated['join_token']},json={'name':'other'}).status_code==200
    assert c.get('/api/rooms/'+r['id'],headers=bh).status_code==200


def test_csrf_and_login(system):
    c,r,b,bh,ss,store=system
    assert c.post('/api/rooms',headers={'Origin':'https://evil.example'},json={'name':'bad'}).status_code==403
    assert c.post('/api/rooms',data='{"name":"bad"}',headers={'Content-Type':'text/plain'}).status_code==415
    assert c.post('/api/logout',json={}).status_code==200
    assert c.get('/api/me').status_code==401
    assert c.post('/api/login',json={'username':'owner','password':'wrong'}).status_code==401
    assert c.post('/api/login',json={'username':'owner','password':'a-long-test-password'}).status_code==200


def test_public_agent_url_and_secure_login_cookie(tmp_path,monkeypatch):
    monkeypatch.setenv('AGENTCHAT_PUBLIC_API_URL','https://chat.example.test/agent-connect/')
    monkeypatch.setenv('AGENTCHAT_SECURE_COOKIE','1')
    app=create_app(tmp_path)
    c=TestClient(app,base_url='https://chat.example.test')
    assert c.get('/api/status').json()['public_api_url']=='https://chat.example.test/agent-connect'
    r=c.post('/api/bootstrap',json={'username':'owner','password':'test-secure-password','setup_code':(tmp_path/'bootstrap-code').read_text()})
    assert r.status_code==200
    assert 'Secure' in r.headers['set-cookie'] and 'HttpOnly' in r.headers['set-cookie']
    assert c.get('/api/me').status_code==200
    for path in ('/','/app.js','/style.css'):
        assert c.get(path).headers['cache-control']=='no-cache'
    monkeypatch.setenv('AGENTCHAT_PUBLIC_API_URL','http://chat.example.test/agent-connect')
    with pytest.raises(ValueError):create_app(tmp_path)


def test_body_limit_including_chunked(system):
    c,r,b,bh,ss,store=system
    assert c.post('/api/rooms',content=b' '*150000,headers={'Content-Type':'application/json'}).status_code==413
    assert c.post('/api/rooms',content=iter([b' '*70000,b' '*70000]),headers={'Content-Type':'application/json'}).status_code==413


def test_downloadable_setup_contains_only_client_source(system):
    import io,zipfile
    c,*_=system
    version=c.get('/api/status').json()['client_version']
    assert len(version)==16 and int(version,16)>=0
    response=c.get('/downloads/agentchat-client.zip?v='+version)
    assert response.headers['cache-control']=='no-store'
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names=archive.namelist()
        assert 'agentchat.py' in names and 'bridge/runner.py' in names
        assert 'bridge/roles.py' in names and 'bridge/names.py' in names
        assert 'bridge/manage.ps1' in names and 'bridge/model_catalog.py' in names
        assert archive.read('skills/agentchat-setup/SKILL.md').decode('utf-8').replace('\r\n','\n')==c.get('/downloads/setup-skill.md').text.replace('\r\n','\n')
        assert not any('data/' in n or '.agentchat/' in n or n.endswith('.env') for n in names)
    assert 'agentchat-setup' in c.get('/downloads/setup-skill.md').text


def test_history_pagination_and_durable_restart(system):
    c,r,b,bh,ss,store=system
    for i in range(7):send(c,r,f'@admin note {i}')
    first=store.history(r['id'],store.identity(b['token']),limit=3)
    second=store.history(r['id'],store.identity(b['token']),after=first[-1]['id'],limit=3)
    assert len(first)==len(second)==3 and second[0]['id']>first[-1]['id']
    from server.core import Store
    reopened=Store(store.path)
    assert len(reopened.history(r['id'],reopened.identity(b['token'])))==7


def test_loop_guard(system):
    c,r,b,bh,ss,store=system
    send(c,r,'@all start')
    a=store.identity(ss[0]['token']);z=store.identity(ss[1]['token'])
    cause=claim(c,bh,ss[0])['message']['id']
    for i in range(8):
        sender,target=(a,'codex1') if i%2==0 else (z,'claude1')
        message=store.post(r['id'],sender,body=f'@{target} hop {i}',nonce=str(i),cause=cause)
        cause=message['id']
    assert message['deliveries'][0]['status']=='skipped'
    assert '上限' in message['deliveries'][0]['error']


def test_role_templates_round_trip_update_and_restart(system):
    from server.core import Store
    c,r,b,bh,ss,store=system
    roles=[{'name':'小克','harness':'claude','role':'审查','model':'sonnet','permission_mode':'default'},
           {'name':'小龙','harness':'command','role':'实现','command':['tool','{workspace}','{session_id}']}]
    response=c.post('/api/role-templates',json={'name':'研究项目','roles':roles})
    assert response.status_code==200,response.text
    saved=response.json()
    assert saved['roles']==roles and saved['revision']==1
    assert c.get('/api/role-templates').json()==[saved]
    assert c.post('/api/role-templates',json={'name':'研究项目','roles':roles}).status_code==409
    changed=[{**roles[0],'model':'opus'},roles[1]]
    updated=c.put('/api/role-templates/'+saved['id'],json={'name':'研究项目','roles':changed,'revision':1})
    assert updated.status_code==200 and updated.json()['revision']==2
    assert c.put('/api/role-templates/'+saved['id'],json={'name':'研究项目','roles':roles,'revision':1}).status_code==409
    owner=c.get('/api/me').json()
    assert Store(store.path).templates(owner)[0]['roles']==changed
    assert c.get('/api/rooms/'+r['id']).json()['members']==store.snapshot(r['id'],owner)['members']


def test_role_templates_authorization_and_validation(system):
    c,r,b,bh,ss,store=system
    roles=[{'name':'小克','harness':'claude','role':'审查'}]
    saved=c.post('/api/role-templates',json={'name':'私有模板','roles':roles}).json()
    anonymous=TestClient(c.app)
    assert anonymous.get('/api/role-templates').status_code==401
    assert c.get('/api/role-templates',headers=bh).status_code==403
    assert c.post('/api/role-templates',headers=bh,json={'name':'无权限','roles':roles}).status_code==403
    outsider={'id':'another-user','kind':'user','name':'other'}
    assert store.templates(outsider)==[]
    with pytest.raises(Problem) as denied:store.save_template(outsider,'修改',roles,saved['id'],1)
    assert denied.value.status==404
    for invalid in ([],[roles[0],roles[0]],[{**roles[0],'name':'../bad'}],[{**roles[0],'harness':'unknown'}],[{**roles[0],'role':''}]):
        response=c.post('/api/role-templates',json={'name':'invalid','roles':invalid})
        assert response.status_code in (400,422)
    assert len(c.get('/api/role-templates').json())==1


def test_template_delete_preserves_room_profile_and_checks_revision(system):
    c,r,b,bh,ss,store=system
    roles=[{'name':'小龙','harness':'codex','role':'开发'}]
    saved=c.post('/api/role-templates',json={'name':'可删除模板','roles':roles}).json()
    c.put(f'/api/rooms/{r["id"]}/setup-profile',json={'workspace':'E:\\workspace','roles':roles,'revision':0}).raise_for_status()
    path='/api/role-templates/'+saved['id']
    headers={'Content-Type':'application/json'}
    assert c.delete(path+'?revision=1',headers={**headers,**bh}).status_code==403
    assert c.delete(path+'?revision=2',headers=headers).status_code==409
    assert c.delete(path+'?revision=1',headers=headers).status_code==200
    assert c.get('/api/role-templates').json()==[]
    assert c.get(f'/api/rooms/{r["id"]}/setup-profile').json()['roles']==roles


def test_persistent_recipient_selection_controls_routing(system):
    c,r,b,bh,ss,store=system
    result=send(c,r,'@claude1\n@codex1 是正文提及',to=['claude1']).json()
    assert [d['name'] for d in result['deliveries']]==['claude1']
    assert not next(m for m in c.get('/api/rooms/'+r['id']).json()['members'] if m['id']==ss[1]['id'])['enabled']
    assert send(c,r,'text',to=['notfound']).status_code==400
    assert send(c,r,'@owner done',headers={'Authorization':'Bearer '+ss[0]['token']},to=['owner']).status_code==403
    data={'body':'same','to':['claude1'],'max_rounds':3,'nonce':'route-key'}
    path=f'/api/rooms/{r["id"]}/messages'
    assert c.post(path,json=data).status_code==200
    assert c.post(path,json=data).status_code==200
    assert c.post(path,json={**data,'to':['codex1']}).status_code==409


def test_round_budget_final_summary_cannot_spawn_next_round(system):
    c,r,b,bh,ss,store=system
    with store.tx() as db:db.execute("UPDATE members SET enabled=1 WHERE kind='agent'")
    c.put(f'/api/rooms/{r["id"]}/round-limit',json={'max_rounds':2}).raise_for_status()
    send(c,r,'@claude1 开始讨论').raise_for_status()
    first=claim(c,bh,ss[0]);assert first['round_budget']=={'mode':'global','current':1,'max':2,'remaining':2,'last':False}
    c.post(f'/api/bridge/deliveries/{first["id"]}/finish',headers=bh,json={'lease':first['lease'],'body':'@codex1 请审查'}).raise_for_status()
    second=claim(c,bh,ss[1]);assert second['round_budget']=={'mode':'global','current':2,'max':2,'remaining':1,'last':True}
    # A changed default only affects the next administrator task.
    c.put(f'/api/rooms/{r["id"]}/round-limit',json={'max_rounds':0}).raise_for_status()
    c.post(f'/api/bridge/deliveries/{second["id"]}/finish',headers=bh,json={'lease':second['lease'],'body':'@claude1 最后决定与总结'}).raise_for_status()
    history=c.get(f'/api/rooms/{r["id"]}/messages').json()
    assert history[-1]['depth']==2 and history[-1]['max_rounds']==2
    assert history[-1]['deliveries'][0]['status']=='skipped'
    assert claim(c,bh,ss[0]) is None
    send(c,r,'@claude1 新任务').raise_for_status()
    assert claim(c,bh,ss[0])['round_budget']['max']==0


def test_zero_round_limit_has_no_old_depth_or_message_cap(system):
    c,r,b,bh,ss,store=system
    root=send(c,r,'@all 开始',max_rounds=0).json()
    actors=[store.identity(s['token']) for s in ss[:2]]
    cause=root['id']
    for index in range(30):
        actor=actors[index%2];target=ss[1-index%2]['name']
        result=store.post(r['id'],actor,body=f'@{target} 第 {index+1} 轮',nonce='unlimited-'+str(index),cause=cause)
        assert result['deliveries'][0]['status']=='pending'
        cause=result['id']


def test_stop_all_cancels_running_queue_and_rejects_late_work(system):
    c,r,b,bh,ss,store=system
    send(c,r,'@all 工作');delivery=claim(c,bh,ss[0]);root=delivery['message']['id']
    path=f'/api/rooms/{r["id"]}/control'
    assert c.post(path,headers=bh,json={'action':'stop-all'}).status_code==403
    stopped=c.post(path,json={'action':'stop-all'}).json();assert stopped['cancelled']==3
    assert c.post('/api/bridge/heartbeat',headers=bh,json={'leases':[{'id':delivery['id'],'lease':delivery['lease']}]}).json()['valid_leases']==[]
    assert claim(c,bh,ss[1]) is None
    assert c.post(f'/api/bridge/deliveries/{delivery["id"]}/finish',headers=bh,json={'lease':delivery['lease'],'body':'@all late'}).status_code==409
    assert send(c,r,'@all late',headers={'Authorization':'Bearer '+ss[0]['token']},cause=root).status_code==409
    send(c,r,'@claude1 新工作').raise_for_status()
    assert claim(c,bh,ss[0]) is not None


def test_message_metadata_is_per_turn_and_survives_model_changes(system):
    c,r,b,bh,ss,store=system
    send(c,r,'@claude1 first');delivery=claim(c,bh,ss[0])
    metadata={'model':'model-a','think_level':'high','harness':'spoof'}
    c.post(f'/api/bridge/deliveries/{delivery["id"]}/finish',headers=bh,json={'lease':delivery['lease'],'body':'@owner done','runtime':metadata}).raise_for_status()
    c.post('/api/bridge/heartbeat',headers=bh,json={'sessions':[{'id':ss[0]['id'],'runtime':{'model':'model-b','think_level':'low'}}]}).raise_for_status()
    history=c.get(f'/api/rooms/{r["id"]}/messages').json()
    assert history[-1]['runtime']=={'harness':'command','model':'model-a','think_level':'high'}
