import base64

from fastapi.testclient import TestClient

from test_system import system

PNG = 'data:image/png;base64,' + base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'\0' * 40).decode()
WEBP = 'data:image/webp;base64,' + base64.b64encode(b'RIFF\0\0\0\0WEBPVP8 ' + b'\0' * 30).decode()


def test_avatars_persist_validate_and_only_owner_can_change(system):
    c,r,b,bh,ss,store=system
    path=f'/api/rooms/{r["id"]}/members/{ss[0]["id"]}/avatar'
    for value in ['icon:claude','text:小龙','text:A',PNG,WEBP,'']:
        result=c.post(path,json={'avatar':value});result.raise_for_status()
        assert result.json()['avatar']==value
    c.post(path,json={'avatar':'icon:deepseek'}).raise_for_status()
    from server.core import Store
    with Store(store.path).tx() as db:assert db.execute('SELECT avatar FROM members WHERE id=?',(ss[0]['id'],)).fetchone()[0]=='icon:deepseek'
    snapshot=c.get('/api/rooms/'+r['id']).json()
    assert next(m for m in snapshot['members'] if m['id']==ss[0]['id'])['avatar']=='icon:deepseek'
    assert all(m['avatar']=='' for m in snapshot['members'] if m['id']!=ss[0]['id'])

    fake_png='data:image/png;base64,'+base64.b64encode(b'<svg onload=alert(1)>').decode()
    bad=['icon:unknown','icon:','text:','text:abc','text:<b','text:a"',fake_png,'data:image/svg+xml;base64,PHN2Zz4=',
         'data:image/png;base64,not base64!','javascript:alert(1)','https://example.org/a.png','data:image/png;base64,'+'A'*30000,None]
    for value in bad:
        assert c.post(path,json={'avatar':value}).status_code==422,value

    human=next(m for m in snapshot['members'] if m['kind']=='user')
    c.post(path.replace(ss[0]['id'],human['id']),json={'avatar':'text:我'}).raise_for_status()
    bridge_id=next(m['id'] for m in snapshot['members'] if m['kind']=='bridge')
    assert c.post(path.replace(ss[0]['id'],bridge_id),json={'avatar':'text:B'}).status_code==404
    for h in [bh,{'Authorization':'Bearer '+ss[0]['token']}]:
        assert c.post(path,headers=h,json={'avatar':'text:X'}).status_code==403
    outsider=TestClient(c.app);outsider.post('/api/register',json={'username':'outside','password':'isolated-long-password'}).raise_for_status()
    assert outsider.post(path,json={'avatar':'text:X'}).status_code==403
    other=c.post('/api/rooms',json={'name':'another room'}).json()
    assert c.post(path.replace(r['id'],other['id']),json={'avatar':'text:X'}).status_code==404
