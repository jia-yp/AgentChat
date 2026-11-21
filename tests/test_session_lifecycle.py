from bridge.common import read_json,write_json
from bridge.runner import Bridge
from tests.test_system import system,send,claim


def test_delete_cancels_work_blocks_credentials_and_preserves_history(system):
    c,room,bridge,bh,sessions,store=system;s=sessions[0];base='/api/rooms/'+room['id']
    send(c,room,'@claude1 initial');first=claim(c,bh,s)
    c.post('/api/bridge/deliveries/'+first['id']+'/finish',headers=bh,json={'lease':first['lease'],'body':'@owner retained reply','native_id':'keep-native'}).raise_for_status()
    send(c,room,'@claude1 running');running=claim(c,bh,s)
    send(c,room,'@claude1 queued')
    c.put(base+'/round-limit',json={'max_rounds':8,'round_mode':'individual','session_rounds':{s['id']:2},'session_speaks':{s['id']:2}}).raise_for_status()
    control=base+'/members/'+s['id']+'/control'
    assert c.post(control,json={'action':'delete'}).status_code==200
    snap=c.get(base).json();deleted=next(m for m in snap['members'] if m['id']==s['id'])
    assert deleted['deleted_at'] and deleted['revoked'] and deleted['restorable']
    assert deleted['native_id']=='keep-native' and not deleted['enabled']
    assert s['id'] not in snap['room']['session_rounds'] and s['id'] not in snap['room']['session_speaks']
    assert 'token_hash' not in deleted and 'has_credential' not in deleted
    assert c.get(base,headers={'Authorization':'Bearer '+s['token']}).status_code==401
    assert c.post('/api/bridge/sessions/'+s['id']+'/claim',headers=bh,json={}).status_code==404
    assert send(c,room,'@claude1 cannot trigger').status_code==400
    heartbeat=c.post('/api/bridge/heartbeat',headers=bh,json={'leases':[{'id':running['id'],'lease':running['lease']}]}).json()
    assert not heartbeat['valid_leases'] and {'id':s['id'],'revoked':True} in heartbeat['session_states']
    late=c.post('/api/bridge/deliveries/'+running['id']+'/finish',headers=bh,json={'lease':running['lease'],'body':'@owner late reply'})
    assert late.status_code in (404,409)
    history=c.get(base+'/messages').json()
    assert any(m['body']=='@owner retained reply' and m['sender_name']=='claude1' for m in history)
    assert all(d['status'] not in ('pending','running') for m in history for d in m['deliveries'])
    assert c.post(control,json={'action':'delete'}).status_code==200
    assert next(m for m in c.get(base).json()['members'] if m['id']==s['id'])['deleted_at']==deleted['deleted_at']
    assert c.post('/api/bridge/sessions',headers=bh,json={'name':'claude1','role':'new','harness':'command'}).status_code==409


def test_restore_waits_for_owner_mention_and_never_replays_work(system):
    c,room,bridge,bh,sessions,store=system;s=sessions[0];base='/api/rooms/'+room['id'];control=base+'/members/'+s['id']+'/control'
    send(c,room,'@all start');d=claim(c,bh,s)
    c.post('/api/bridge/heartbeat',headers=bh,json={'sessions':[{'id':s['id'],'native_id':'same-native','lifecycle_supported':True}]}).raise_for_status()
    c.post(control,json={'action':'delete'}).raise_for_status()
    receipt=c.post(control,json={'action':'restore'});receipt.raise_for_status()
    assert receipt.json()['requires_bridge_restart'] is False
    restored=next(m for m in c.get(base).json()['members'] if m['id']==s['id'])
    assert not restored['deleted_at'] and not restored['revoked'] and not restored['enabled']
    assert restored['native_id']=='same-native' and claim(c,bh,s) is None
    assert c.get(base,headers={'Authorization':'Bearer '+s['token']}).status_code==200
    peer=claim(c,bh,sessions[1]);assert peer
    response=send(c,room,'@claude1 peer cannot reactivate',{'Authorization':'Bearer '+sessions[1]['token']},cause=peer['message']['id'])
    assert response.json()['deliveries'][0]['status']=='skipped'
    send(c,room,'@claude1 owner resumes').raise_for_status();next_task=claim(c,bh,s)
    assert next_task['message']['body']=='@claude1 owner resumes' and next_task['id']!=d['id']
    c.post(control,json={'action':'restore'}).raise_for_status()
    assert next(m for m in c.get(base).json()['members'] if m['id']==s['id'])['enabled']


def test_lifecycle_is_owner_only_and_revoked_bridge_cannot_be_restored(system):
    c,room,bridge,bh,sessions,store=system;base='/api/rooms/'+room['id'];s=sessions[0]
    manager=c.post('/api/bridge/sessions',headers=bh,json={'name':'manager','role':'manage','harness':'command','session_type':'manager'}).json()
    for headers in (bh,{'Authorization':'Bearer '+s['token']},{'Authorization':'Bearer '+manager['token']}):
        for action in ('delete','restore'):
            assert c.post(base+'/members/'+s['id']+'/control',headers=headers,json={'action':action}).status_code==403
    assert c.post(base+'/members/'+bridge['member_id']+'/control',json={'action':'delete'}).status_code==404
    c.post(base+'/members/'+s['id']+'/control',json={'action':'delete'}).raise_for_status()
    c.post(base+'/members/'+bridge['member_id']+'/control',json={'action':'revoke'}).raise_for_status()
    assert not next(m for m in c.get(base).json()['members'] if m['id']==s['id'])['restorable']
    assert c.post(base+'/members/'+s['id']+'/control',json={'action':'restore'}).status_code==409


def test_manager_history_stays_private_after_deletion(system):
    c,room,bridge,bh,sessions,store=system;base='/api/rooms/'+room['id']
    manager=c.post('/api/bridge/sessions',headers=bh,json={'name':'manager','role':'manage','harness':'command','session_type':'manager'}).json()
    send(c,room,'@manager private history').raise_for_status()
    c.post(base+'/members/'+manager['id']+'/control',json={'action':'delete'}).raise_for_status()
    assert c.get(base+'/messages?channel='+manager['id']).json()[0]['body']=='@manager private history'
    for headers in (bh,{'Authorization':'Bearer '+sessions[0]['token']}):
        assert all('private history' not in m['body'] for m in c.get(base+'/messages',headers=headers).json())
        assert c.get(base+'/messages?channel='+manager['id'],headers=headers).status_code==403
    replacement=c.post('/api/bridge/sessions',headers=bh,json={'name':'new-manager','role':'manage','harness':'command','session_type':'manager'})
    assert replacement.status_code==200
    assert c.post(base+'/members/'+manager['id']+'/control',json={'action':'restore'}).status_code==409


def test_old_bridge_restore_explicitly_requires_restart(system):
    c,room,bridge,bh,sessions,store=system;path='/api/rooms/'+room['id']+'/members/'+sessions[0]['id']+'/control'
    c.post(path,json={'action':'delete'}).raise_for_status()
    assert c.post(path,json={'action':'restore'}).json()['requires_bridge_restart'] is True


def test_delete_settles_synchronous_waiting_tasks_and_pending_steer(system):
    c,room,bridge,bh,sessions,store=system;base='/api/rooms/'+room['id']
    # A pending note cannot survive deletion and attach to a restored turn.
    with store.tx() as db:db.execute("UPDATE members SET harness='codex' WHERE id=?",(sessions[0]['id'],))
    c.post('/api/bridge/heartbeat',headers=bh,json={'sessions':[{'id':s['id'],'sync_supported':True,'steer_supported':True} for s in sessions]}).raise_for_status()
    root=send(c,room,'@all synchronized',round_mode='synchronous',max_rounds=2).json()
    tasks=[claim(c,bh,s) for s in sessions]
    send(c,room,'@claude1 pending steer',delivery_mode='steer',steer_task=tasks[0]['id']+':1').raise_for_status()
    send(c,room,'@claude1 next round',{'Authorization':'Bearer '+sessions[1]['token']},cause=root['id']).raise_for_status()
    c.post(base+'/members/'+sessions[0]['id']+'/control',json={'action':'delete'}).raise_for_status()
    history=c.get(base+'/messages').json()
    note=next(m for m in history if m['delivery_mode']=='steer')
    assert note['steers'][0]['status']=='rejected'
    with store.tx() as db:
        assert not db.execute("SELECT 1 FROM deliveries WHERE member=? AND status IN ('pending','running','bundled','round_waiting')",(sessions[0]['id'],)).fetchone()


def test_bridge_syncs_deleted_and_restored_sessions_without_changing_native_id(tmp_path):
    context=tmp_path/'session.json';config=tmp_path/'bridge.json'
    session={'id':'worker','name':'worker','token':'worker-token','context':str(context),'workspace':str(tmp_path),
             'native_id':'same-native','definition':{'harness':'command','command':['fixture']}}
    write_json(context,session);write_json(config,{'url':'http://127.0.0.1:1','token':'test','workspace':str(tmp_path),'room':{'name':'test'},'sessions':[{'id':'worker','context':str(context)}]})
    bridge=Bridge(config)
    class Client:
        revoked=True
        def call(self,path,payload):
            if self.revoked:assert payload['sessions'][0]['lifecycle_supported'] is True
            else:assert payload['sessions']==[]  # Revoked identities are not reported as live.
            return {'valid_leases':[],'session_states':[{'id':'worker','revoked':self.revoked}]}
    bridge.client=Client();bridge.heartbeat()
    assert read_json(context)['revoked'] and read_json(context)['native_id']=='same-native'
    bridge.client.revoked=False;bridge.heartbeat()
    assert not read_json(context)['revoked'] and read_json(context)['native_id']=='same-native'
