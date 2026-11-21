import json

import pytest
from fastapi.testclient import TestClient

from server.core import Store
from tests.test_system import system, send, claim


PASSWORD='guest-invitation-test-password'


def register(owner,name='guest'):
    client=TestClient(owner.app)
    result=client.post('/api/register',json={'username':name,'password':PASSWORD})
    assert result.status_code==201,result.text
    return client,result.json()['id']


def invite(owner,room,role='observer',rotate=False):
    result=owner.post(f"/api/rooms/{room['id']}/guest-invitation",json={'role':role,'rotate':rotate})
    assert result.status_code==200,result.text
    return result.json()


def join(client,link,name=None):
    result=client.post('/api/guest-invitations/join',json={'token':link['token'],'name':name})
    assert result.status_code==200,result.text
    return result.json()


def patch(client,room,mid,**data):
    return client.patch(f"/api/rooms/{room['id']}/guests/{mid}",json=data)


def test_registered_join_roles_rotation_and_kick_rejoin(system):
    c,r,b,bh,ss,store=system;guest,uid=register(c);mid=uid+':'+r['id'];base=f"/api/rooms/{r['id']}"
    assert c.get(base+'/guest-invitation').json() is None
    link=invite(c,r)
    assert TestClient(c.app).post('/api/guest-invitations/preview',json={'token':link['token']}).status_code==401
    assert guest.get(base).status_code==403
    preview=guest.post('/api/guest-invitations/preview',json={'token':link['token']}).json()
    assert preview['name']==r['name'] and not preview['joined'] and preview['role']=='observer'
    assert join(guest,link)['role']=='observer'
    assert guest.get('/api/rooms').json()[0]['role']=='observer'
    updated=invite(c,r,'participant')
    assert updated['token']==link['token']
    assert join(guest,updated)['role']=='observer'  # Links cannot overwrite member-card decisions.
    assert patch(c,r,mid,role='participant').status_code==200
    rotated=invite(c,r,'observer',True)
    assert rotated['token']!=link['token']
    assert guest.post('/api/guest-invitations/join',json={'token':link['token']}).status_code==404
    assert guest.get(base).json()['viewer']['role']=='participant'
    assert patch(c,r,mid,kick=True).status_code==200
    assert guest.get(base).status_code==403 and guest.get('/api/rooms').json()==[]
    assert send(guest,r,'@all forbidden').status_code==403
    assert join(guest,rotated)['role']=='observer'
    assert guest.get(base).json()['viewer']=={'role':'observer','member_id':mid}
    assert send(guest,r,'@all still observer').status_code==403
    assert Store(store.path).snapshot(r['id'],store.identity(cookie=guest.cookies.get('agentchat_session')))['viewer']['role']=='observer'


@pytest.mark.parametrize('role',['observer','participant'])
def test_manager_privacy_on_every_read_surface(system,role):
    c,r,b,bh,ss,store=system;g,uid=register(c);join(g,invite(c,r,role));base=f"/api/rooms/{r['id']}"
    manager=c.post('/api/bridge/sessions',headers=bh,json={'name':'secret_manager','harness':'codex','role':'private workspace secret','session_type':'manager'}).json()
    private=send(c,r,'@secret_manager TOP_SECRET_PRIVATE').json()
    public=send(c,r,'@all public searchable').json()
    with store.tx() as db:
        # A malformed legacy quote must still not expose its private source.
        db.execute('UPDATE messages SET reply_to=?,session_rounds=? WHERE id=?',(private['id'],json.dumps({manager['id']:9}),public['id']))
    snap=g.get(base).json()
    assert 'setup' not in snap and manager['id'] not in json.dumps(snap)
    assert all(m['kind']!='bridge' and 'native_id' not in m and 'permission_settings' not in m for m in snap['members'])
    paths=[base+'/messages',base+'/timeline',base+'/search?q=TOP_SECRET_PRIVATE',base+'/search?q=public']
    for path in paths:
        result=g.get(path);assert result.status_code==200,result.text
        assert 'TOP_SECRET_PRIVATE' not in result.text and 'secret_manager' not in result.text and manager['id'] not in result.text
    history=g.get(base+'/messages').json();assert [m['id'] for m in history]==[public['id']]
    assert history[0]['quote'] is None and history[0]['session_rounds']=={}
    batch=g.post(base+'/reading/messages',json={'ids':[private['id'],public['id']]}).json()
    assert [m['id'] for m in batch]==[public['id']]
    assert g.get(base+'/messages?channel='+manager['id']).status_code==403
    assert g.get(base+'/search?q=secret&scope=private').status_code==403
    assert g.post(base+'/summaries',json={'root':public['id'],'round':1}).status_code==403
    assert send(g,r,'@secret_manager attempt').status_code in (400,403)
    assert send(g,r,'@all quote',reply_to=private['id']).status_code in (400,403)
    assert send(g,r,'@all cause',cause=private['id']).status_code in (400,403)
    assert 'TOP_SECRET_PRIVATE' in c.get(base+'/messages').text
    # A manager cannot target a guest, even though both owner and guest are humans.
    claim(c,bh,manager)
    denied=send(c,r,'@guest forbidden',headers={'Authorization':'Bearer '+manager['token']},cause=private['id'])
    assert denied.status_code==400,denied.text
    assert send(c,r,'@secret_manager @guest mixed').status_code==400


def test_participant_round_trip_observer_and_live_role_change(system):
    c,r,b,bh,ss,store=system;g,uid=register(c);mid=uid+':'+r['id'];join(g,invite(c,r))
    assert send(g,r,'@claude1 observer').status_code==403
    assert patch(c,r,mid,role='participant').status_code==200
    message=send(g,r,'@claude1 task',max_rounds=3).json();assert message['sender']==mid
    d=claim(c,bh,ss[0]);assert d['message']['id']==message['id']
    answer=send(c,r,'@guest completed',headers={'Authorization':'Bearer '+ss[0]['token']},cause=message['id'])
    assert answer.status_code==200,answer.text
    assert answer.json()['id'] in [m['id'] for m in g.get(f"/api/rooms/{r['id']}/messages").json()]
    queued=send(g,r,'@codex1 queue').json()
    assert g.post(f"/api/rooms/{r['id']}/messages/{queued['id']}/recall",json={}).status_code==200
    owner_msg=send(c,r,'@codex2 owner queue').json()
    assert g.post(f"/api/rooms/{r['id']}/messages/{owner_msg['id']}/recall",json={}).status_code==403
    assert patch(c,r,mid,role='observer').status_code==200
    assert send(g,r,'@all blocked immediately').status_code==403


@pytest.mark.parametrize('role',['observer','participant'])
def test_guests_cannot_manage_sessions_room_or_other_people(system,role):
    c,r,b,bh,ss,store=system;g,uid=register(c);join(g,invite(c,r,role));base=f"/api/rooms/{r['id']}";agent=ss[0]['id']
    owner_mid=c.get('/api/me').json()['id']+':'+r['id']
    other=g.post('/api/rooms',json={'name':'Guests can own their own rooms'}).json()
    assert g.get('/api/rooms/'+other['id']+'/setup-profile').status_code==200
    attempts=[
        ('GET','/guest-invitation',None),('POST','/guest-invitation',{'role':'participant'}),
        ('GET','/setup-profile',None),('GET','/session-changes',None),
        ('PUT','/setup-profile',{'workspace':'','roles':[{'name':'new_worker','harness':'codex','role':'test'}],'revision':0,'inherit_manager':True}),
        ('POST','/control',{'action':'rotate-token'}),('POST','/control',{'action':'stop-all'}),('POST','/control',{'action':'pause'}),
        ('PUT','/round-limit',{'max_rounds':1}),
        ('POST','/synchronous/continue',{'root':1,'round':1}),
        ('POST',f'/members/{agent}/control',{'action':'enable'}),('POST',f'/members/{agent}/control',{'action':'delete'}),
        ('POST',f'/members/{agent}/control',{'action':'restore'}),('POST',f'/members/{agent}/models/refresh',{}),
        ('POST',f'/members/{agent}/model',{'model':'test','revision':0}),
        ('POST',f'/members/{agent}/permissions',{'mode':'full-access','revision':0}),
        ('POST',f'/members/{agent}/appearance',{'bubble_color':'#eeeeee'}),('POST',f'/members/{owner_mid}/avatar',{'avatar':'text:NO'}),
        ('POST','/deliveries/nonexistent/retry',{}),
    ]
    for method,path,data in attempts:
        result=g.request(method,base+path,json=data)
        assert result.status_code==403,(path,result.status_code,result.text)
    assert patch(g,r,owner_mid,name='hijacked').status_code==403
    assert patch(g,r,uid+':'+r['id'],role='participant').status_code==403
    assert patch(g,r,owner_mid,kick=True).status_code==403
    assert patch(c,r,owner_mid,kick=True).status_code==400
    assert patch(c,r,owner_mid,role='observer').status_code==400
    assert patch(c,r,uid+':'+r['id'],role='owner').status_code==422


def test_guest_profile_scoped_to_room_and_creator_can_edit(system):
    c,r,b,bh,ss,store=system;g,uid=register(c);link=invite(c,r);join(g,link,'访客');mid=uid+':'+r['id'];base=f"/api/rooms/{r['id']}"
    assert patch(g,r,mid,name='来宾').status_code==200
    assert g.get('/api/me').json()['name']=='guest'  # Account identity stays unchanged.
    assert g.post(base+f'/members/{mid}/avatar',json={'avatar':'text:访'}).status_code==200
    assert g.post(base+f'/members/{mid}/appearance',json={'bubble_color':'#eee5f8'}).status_code==200
    assert patch(c,r,mid,name='客人').status_code==200
    assert c.post(base+f'/members/{mid}/avatar',json={'avatar':'icon:codex'}).status_code==200
    assert c.post(base+f'/members/{mid}/appearance',json={'bubble_color':'#e3effb'}).status_code==200
    person=next(m for m in g.get(base).json()['members'] if m['id']==mid)
    assert (person['name'],person['avatar'],person['bubble_color'])==('客人','icon:codex','#e3effb')
    assert patch(g,r,mid,name='admin').status_code==400
    assert patch(g,r,mid,name=ss[0]['name']).status_code==409
    assert patch(g,r,mid,name='<script>').status_code==400
    assert patch(g,r,ss[0]['id'],name='renamed').status_code==404
    own=g.post('/api/rooms',json={'name':'different room'}).json()
    assert g.post(f"/api/rooms/{own['id']}/members/{mid}/appearance",json={'bubble_color':'#ffffff'}).status_code==404
    assert patch(c,r,mid,kick=True).status_code==200
    assert g.post(base+f'/members/{mid}/avatar',json={'avatar':'text:访'}).status_code==403
    assert join(g,link)['role']=='observer'
    restored=next(m for m in g.get(base).json()['members'] if m['id']==mid)
    assert restored['name']=='客人' and restored['bubble_color']=='#e3effb'


def test_alias_collision_and_tokens_are_separate_credentials(system):
    c,r,b,bh,ss,store=system;g,uid=register(c);link=invite(c,r)
    assert g.post('/api/guest-invitations/join',json={'token':link['token'],'name':'claude1'}).status_code==409
    assert g.get('/api/rooms').json()==[]
    assert g.post('/api/guest-invitations/join',json={'token':r['join_token']}).status_code==404
    assert g.post('/api/agent/join',headers={'Authorization':'Bearer '+link['token']},json={'name':'not_bridge'}).status_code==401
    assert c.post(f"/api/rooms/{r['id']}/guest-invitation",headers=bh,json={'role':'observer'}).status_code==403
    join(g,link)
    assert c.get(f"/api/rooms/{r['id']}/guest-invitation").json()['token']==link['token']
    assert link['token'] not in g.get('/api/rooms').text
    assert link['token'] not in g.get(f"/api/rooms/{r['id']}").text


def test_participant_can_initiate_synchronous_conversation(system):
    c,r,b,bh,ss,store=system;g,uid=register(c);join(g,invite(c,r,'participant'))
    c.post('/api/bridge/heartbeat',headers=bh,json={'sessions':[{'id':s['id'],'sync_supported':True} for s in ss]}).raise_for_status()
    result=send(g,r,'@all discuss',round_mode='synchronous',max_rounds=2,session_speaks={ss[0]['id']:2})
    assert result.status_code==200,result.text
    view=g.get(f"/api/rooms/{r['id']}").json()['synchronous']
    assert view['round']==1 and len(view['participants'])==3 and 'recovering' not in view


@pytest.mark.parametrize('change',['rename','kick'])
def test_running_reply_survives_human_name_or_membership_change(system,change):
    c,r,b,bh,ss,store=system;g,uid=register(c);join(g,invite(c,r,'participant'));mid=uid+':'+r['id']
    send(g,r,'@claude1 task').raise_for_status();delivery=claim(c,bh,ss[0])
    patch(c,r,mid,**({'name':'renamed'} if change=='rename' else {'kick':True})).raise_for_status()
    result=c.post(f"/api/bridge/deliveries/{delivery['id']}/finish",headers=bh,json={'lease':delivery['lease'],'body':'@guest task complete'})
    assert result.status_code==200,result.text
    history=c.get(f"/api/rooms/{r['id']}/messages").json()
    assert history[-1]['body']=='@guest task complete' and history[-1]['targets']==['renamed' if change=='rename' else 'guest']
    if change=='kick':assert g.get(f"/api/rooms/{r['id']}").status_code==403
