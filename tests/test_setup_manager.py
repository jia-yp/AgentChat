from argparse import Namespace
import json

import pytest

from bridge.setup_manager import creator_identity, manager_plan, manager_definition


def test_codex_identity_uses_current_thread_not_installed_clis(tmp_path):
    native='11111111-2222-3333-4444-555555555555'
    data=creator_identity(Namespace(),{'CODEX_THREAD_ID':native,'CODEX_SESSION_ID':'different','CODEX_HOME':str(tmp_path)})
    assert data=={'harness':'codex','native_id':native,'codex_source_home':str(tmp_path.resolve())}
    with pytest.raises(ValueError,match='无法确定'):creator_identity(Namespace(),{})
    with pytest.raises(ValueError,match='无法确定'):creator_identity(Namespace(),{'CODEX_THREAD_ID':native,'CLAUDECODE':'1'})


def test_missing_current_id_does_not_create_a_replacement_manager():
    with pytest.raises(ValueError,match='原生 session ID'):creator_identity(Namespace(),{'CLAUDECODE':'1'})
    with pytest.raises(ValueError,match='UUID'):creator_identity(Namespace(manager_harness='claude',manager_native_id='latest'),{})


def test_explicit_creator_metadata_and_pending_recovery(tmp_path):
    data={'harness':'command','native_id':'creator-thread','command':['fixture','{session_id}']}
    path=tmp_path/'creator.json';path.write_text(json.dumps(data),encoding='utf-8')
    assert creator_identity(Namespace(manager_identity=str(path)),{})==data
    assert manager_plan(Namespace(),None,[],{'manager_identity':data},environment={})==data
    path.write_text(json.dumps({**data,'permissions':'full'}),encoding='utf-8')
    with pytest.raises(ValueError,match='身份文件'):creator_identity(Namespace(manager_identity=str(path)),{})


def test_existing_manager_is_reused_without_rebinding_native_id(tmp_path):
    path=tmp_path/'session.json';path.write_text(json.dumps({'name':'manager','native_id':'existing-native','session_type':'manager','definition':{'harness':'codex'}}),encoding='utf-8')
    config={'sessions':[{'name':'manager','context':str(path)}]}
    assert manager_plan(Namespace(manager_harness='claude',manager_native_id='not-used'),config,[{'name':'worker'}],environment={}) is None
    with pytest.raises(ValueError,match='已是 manager'):manager_plan(Namespace(),config,[{'name':'manager'}],environment={})
    assert json.loads(path.read_text())['native_id']=='existing-native'


def test_worker_native_session_cannot_also_become_private_manager(tmp_path):
    path=tmp_path/'worker.json';path.write_text(json.dumps({'name':'worker','native_id':'creator','session_type':'worker','definition':{'harness':'command'}}),encoding='utf-8')
    config={'sessions':[{'context':str(path)}]}
    with pytest.raises(ValueError,match='同时'):
        manager_plan(Namespace(manager_harness='command',manager_native_id='creator'),config,[],environment={})


def test_automatic_manager_alias_avoids_worker_and_other_bridge_collisions():
    identity={'harness':'codex','native_id':'exact-native'}
    assert manager_definition(identity,[],[])['name']=='manager'
    assert manager_definition(identity,[{'name':'manager'}],[{'name':'manager2'}])['name']=='manager3'
    role=manager_definition(identity,[],[{'name':'another-bridge','kind':'bridge','id':'other'}],'abcdef123456')
    assert role['name']=='manager-abcdef' and role['session_type']=='manager'
    assert 'native_id' not in role and role['harness']=='codex'
