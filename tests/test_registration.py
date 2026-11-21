from fastapi.testclient import TestClient

from server.app import create_app
from server.core import password_ok
from tests.test_system import system,send


PASSWORD='public-registration-test-password'


def test_register_logs_in_and_password_is_hashed(system):
    owner,room,bridge,bh,sessions,store=system
    client=TestClient(owner.app)
    assert client.get('/api/status').json()['registration_open'] is True
    result=client.post('/api/register',json={'username':'New_User','password':PASSWORD})
    assert result.status_code==201,result.text
    assert result.json()['name']=='new_user' and result.json()['kind']=='user'
    assert 'HttpOnly' in result.headers['set-cookie'] and 'SameSite=strict' in result.headers['set-cookie']
    assert PASSWORD not in result.text
    assert client.get('/api/rooms').json()==[]
    assert client.get('/api/role-templates').json()==[]
    with store.tx() as db:
        row=db.execute('SELECT * FROM users WHERE name=?',('new_user',)).fetchone()
        assert row['password']!=PASSWORD and password_ok(PASSWORD,row['password'])
    client.post('/api/logout',json={}).raise_for_status()
    assert client.get('/api/me').status_code==401
    assert client.post('/api/login',json={'username':'NEW_USER','password':PASSWORD}).status_code==200
    assert client.get('/api/me').json()['id']==result.json()['id']


def test_registration_does_not_replace_bootstrap_and_can_be_closed(tmp_path,monkeypatch):
    client=TestClient(create_app(tmp_path))
    assert not client.get('/api/status').json()['registration_open']
    assert client.post('/api/register',json={'username':'visitor','password':PASSWORD}).status_code==409
    client.post('/api/bootstrap',json={'username':'owner','password':PASSWORD,'setup_code':(tmp_path/'bootstrap-code').read_text()}).raise_for_status()
    monkeypatch.setenv('AGENTCHAT_REGISTRATION_OPEN','0')
    closed=TestClient(create_app(tmp_path))
    assert not closed.get('/api/status').json()['registration_open']
    assert closed.post('/api/register',json={'username':'visitor','password':PASSWORD}).status_code==403
    assert closed.post('/api/login',json={'username':'owner','password':PASSWORD}).status_code==200


def test_registration_validates_duplicates_passwords_and_origins(system):
    owner,*_=system;c=TestClient(owner.app)
    assert c.post('/api/register',json={'username':'owner','password':PASSWORD}).status_code==409
    assert c.post('/api/register',json={'username':'OWNER','password':PASSWORD}).status_code==409
    assert c.post('/api/register',json={'username':'admin','password':PASSWORD}).status_code==400
    assert c.post('/api/register',json={'username':'bad name','password':PASSWORD}).status_code==400
    assert c.post('/api/register',json={'username':'visitor','password':'short'}).status_code==422
    assert c.post('/api/register',headers={'Origin':'https://foreign.example'},json={'username':'visitor','password':PASSWORD}).status_code==403
    assert c.post('/api/register',content='{}',headers={'Content-Type':'text/plain'}).status_code==415
    assert c.get('/api/me').status_code==401


def test_registration_is_rate_limited(system):
    owner,*_=system;c=TestClient(owner.app)
    results=[c.post('/api/register',json={'username':'owner','password':PASSWORD}).status_code for _ in range(12)]
    assert 409 in results and results[-1]==429


def test_registration_cookie_is_secure_on_public_site(tmp_path,monkeypatch):
    monkeypatch.setenv('AGENTCHAT_SECURE_COOKIE','1')
    c=TestClient(create_app(tmp_path),base_url='https://chat.example')
    c.post('/api/bootstrap',json={'username':'owner','password':PASSWORD,'setup_code':(tmp_path/'bootstrap-code').read_text()}).raise_for_status()
    result=c.post('/api/register',json={'username':'visitor','password':PASSWORD})
    assert result.status_code==201 and 'Secure' in result.headers['set-cookie']
    assert c.get('/api/me').json()['name']=='visitor'


def test_registered_users_cannot_read_or_change_another_users_data(system):
    owner,room,bridge,bh,sessions,store=system
    other=TestClient(owner.app)
    other.post('/api/register',json={'username':'visitor','password':PASSWORD}).raise_for_status()
    own_room=other.post('/api/rooms',json={'name':'Visitor room'}).json()
    assert [r['id'] for r in other.get('/api/rooms').json()]==[own_room['id']]
    assert [r['id'] for r in owner.get('/api/rooms').json()]==[room['id']]
    base='/api/rooms/'+room['id'];member=base+'/members/'+sessions[0]['id']
    send(owner,room,'@owner owner-only content').raise_for_status()
    roles=[{'name':'worker','harness':'codex','role':'private role'}]
    template=owner.post('/api/role-templates',json={'name':'Owner template','roles':roles}).json()
    owner.put(base+'/setup-profile',json={'workspace':'/private/project','project_name':'Private project','roles':roles,'revision':0}).raise_for_status()
    for path in (base,base+'/messages',base+'/messages?channel='+sessions[0]['id'],base+'/setup-profile'):
        response=other.get(path);assert response.status_code==403 and 'owner-only content' not in response.text
    for action in ('delete','restore','disable','revoke'):
        assert other.post(member+'/control',json={'action':action}).status_code==403
    assert other.post(base+'/control',json={'action':'rotate-token'}).status_code==403
    assert other.post(base+'/messages',json={'body':'@owner attack','nonce':'attack'}).status_code==403
    assert other.put(base+'/setup-profile',json={'workspace':'/foreign','roles':roles,'revision':1}).status_code==403
    assert other.get('/api/role-templates').json()==[]
    assert other.put('/api/role-templates/'+template['id'],json={'name':'stolen','roles':roles,'revision':1}).status_code==404
    assert other.delete('/api/role-templates/'+template['id']+'?revision=1',headers={'Content-Type':'application/json'}).status_code==404
    assert owner.get(member.rsplit('/members/',1)[0]).status_code==200
    assert other.post('/api/bridge/sessions',json={'name':'forged','role':'x','harness':'codex'}).status_code==403


def test_one_account_cannot_exhaust_other_logins_behind_same_proxy(system):
    owner,*_=system;c=TestClient(owner.app)
    c.post('/api/register',json={'username':'visitor','password':PASSWORD}).raise_for_status()
    for _ in range(10):assert c.post('/api/login',json={'username':'visitor','password':'wrong'}).status_code==401
    assert c.post('/api/login',json={'username':'visitor','password':'wrong'}).status_code==429
    assert c.post('/api/login',json={'username':'owner','password':'a-long-test-password'}).status_code==200
