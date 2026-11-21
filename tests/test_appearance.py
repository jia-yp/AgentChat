from fastapi.testclient import TestClient

from test_system import system


def test_session_colors_persist_are_validated_and_only_owner_can_change(system):
    c,r,b,bh,ss,store=system
    path=f'/api/rooms/{r["id"]}/members/{ss[0]["id"]}/appearance'
    result=c.post(path,json={'bubble_color':'#DDAAFF'});result.raise_for_status()
    assert result.json()['bubble_color']=='#ddaaff'
    from server.core import Store
    reopened=Store(store.path)
    with reopened.tx() as db:assert db.execute('SELECT bubble_color FROM members WHERE id=?',(ss[0]['id'],)).fetchone()[0]=='#ddaaff'
    snapshot=c.get('/api/rooms/'+r['id']).json()
    assert next(m for m in snapshot['members'] if m['id']==ss[0]['id'])['bubble_color']=='#ddaaff'
    assert all(m['bubble_color']=='' for m in snapshot['members'] if m['id']!=ss[0]['id'])
    for value in ['red','#123','url(https://example.org)',';color:red','#ffffff\n',None]:
        assert c.post(path,json={'bubble_color':value}).status_code==422
    for h in [bh,{'Authorization':'Bearer '+ss[0]['token']}]:
        assert c.post(path,headers=h,json={'bubble_color':'#ffffff'}).status_code==403
    outsider=TestClient(c.app);outsider.post('/api/register',json={'username':'outside','password':'isolated-long-password'}).raise_for_status()
    assert outsider.post(path,json={'bubble_color':'#ffffff'}).status_code==403
    other=c.post('/api/rooms',json={'name':'another room'}).json()
    assert c.post(path.replace(r['id'],other['id']),json={'bubble_color':'#ffffff'}).status_code==404
    bridge_id=next(m['id'] for m in snapshot['members'] if m['kind']=='bridge')
    assert c.post(path.replace(ss[0]['id'],bridge_id),json={'bubble_color':'#ffffff'}).status_code==404
    c.post(path,json={'bubble_color':'#ffffff'}).raise_for_status()
    c.post(path,json={'bubble_color':''}).raise_for_status()
    assert next(m for m in c.get('/api/rooms/'+r['id']).json()['members'] if m['id']==ss[0]['id'])['bubble_color']==''
