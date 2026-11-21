from argparse import Namespace
import json
import os
import sqlite3

import pytest

from bridge.setup_manager import creator_identity, manager_definition
from bridge.setup_project import identity_location, setup_location, workspace_path
from server.core import Store


NATIVE='11111111-2222-3333-4444-555555555555'


def codex_home(tmp_path,cwd,project_id=None):
    home=tmp_path/'codex-home';home.mkdir()
    with sqlite3.connect(home/'state_5.sqlite') as db:
        db.executescript('''CREATE TABLE threads(id TEXT,cwd TEXT,project_id TEXT,model TEXT,reasoning_effort TEXT,title TEXT);
            CREATE TABLE projects(id TEXT,name TEXT); CREATE TABLE project_roots(project_id TEXT,path TEXT);''')
        db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?)',(NATIVE,str(cwd),project_id,'test-model','high','This is the session title, not the project name'))
        db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?)',('a-later-thread',str(tmp_path/'wrong'),None,'wrong-model','low','Latest is not current'))
    return home


@pytest.mark.parametrize('direct',[True,False])
def test_inherits_exact_codex_owning_project_not_title_or_connect_cwd(tmp_path,monkeypatch,direct):
    workspace=tmp_path/'directory-name'/'subfolder';workspace.mkdir(parents=True)
    home=codex_home(tmp_path,workspace,'owner' if direct else None)
    with sqlite3.connect(home/'state_5.sqlite') as db:
        db.execute('INSERT INTO projects VALUES(?,?)',('owner','自定义 Project 名称'))
        db.execute('INSERT INTO project_roots VALUES(?,?)',('owner',str(workspace.parent)))
        db.execute('INSERT INTO projects VALUES(?,?)',('outer','外层 project'))
        db.execute('INSERT INTO project_roots VALUES(?,?)',('outer',str(tmp_path)))
    monkeypatch.chdir(tmp_path)
    identity=creator_identity(Namespace(),{'CODEX_HOME':str(home),'CODEX_THREAD_ID':NATIVE})
    assert identity_location(identity)==(workspace,'自定义 Project 名称')
    assert identity['model']=='test-model' and identity['reasoning_effort']=='high'
    role=manager_definition(identity,[],[])
    assert not {'workspace','project_name','project_name_required','native_id'} & role.keys()


def test_cli_without_named_project_uses_native_workspace_folder(tmp_path):
    workspace=tmp_path/'native-project';workspace.mkdir()
    home=codex_home(tmp_path,workspace)
    with sqlite3.connect(home/'state_5.sqlite') as db:
        db.execute('DROP TABLE projects');db.execute('DROP TABLE project_roots')
    identity=creator_identity(Namespace(),{'CODEX_HOME':str(home),'CODEX_THREAD_ID':NATIVE})
    assert identity_location(identity)==(workspace,'native-project')


def test_ambiguous_project_root_requires_own_project_name(tmp_path):
    home=codex_home(tmp_path,tmp_path)
    with sqlite3.connect(home/'state_5.sqlite') as db:
        for id in ('one','two'):
            db.execute('INSERT INTO projects VALUES(?,?)',(id,id))
            db.execute('INSERT INTO project_roots VALUES(?,?)',(id,str(tmp_path)))
    env={'CODEX_HOME':str(home),'CODEX_THREAD_ID':NATIVE}
    with pytest.raises(ValueError,match='多个 project'):identity_location(creator_identity(Namespace(),env))
    assert identity_location(creator_identity(Namespace(manager_project_name='two'),env))==(tmp_path,'two')


def test_missing_metadata_requires_explicit_current_workspace(tmp_path):
    args=Namespace(manager_harness='command',manager_native_id='exact-native')
    with pytest.raises(ValueError,match='--manager-workspace'):identity_location(creator_identity(args,{}))
    args.manager_workspace=str(tmp_path);args.manager_project_name='Creator project'
    assert identity_location(creator_identity(args,{}))==(tmp_path,'Creator project')
    with pytest.raises(ValueError,match='绝对路径'):workspace_path('relative-folder')


@pytest.mark.skipif(os.name!='nt',reason='Windows native path format')
def test_normalizes_windows_extended_paths(tmp_path):
    assert workspace_path('\\\\?\\'+str(tmp_path))==tmp_path


def test_claude_reads_only_exact_session_metadata(tmp_path):
    folder=tmp_path/'projects'/'encoded-workspace';folder.mkdir(parents=True)
    (folder/(NATIVE+'.jsonl')).write_text(json.dumps({'sessionId':NATIVE,'cwd':str(folder),'type':'user'}),encoding='utf-8')
    identity=creator_identity(Namespace(),{'CLAUDE_SESSION_ID':NATIVE,'CLAUDE_CONFIG_DIR':str(tmp_path)})
    assert identity_location(identity)==(folder,folder.name)


def test_saved_manager_and_claimed_location_are_reused_without_caller_identity(tmp_path):
    context=tmp_path/'session.json';context.write_text(json.dumps({'session_type':'manager','native_id':'old','name':'manager'}),encoding='utf-8')
    config={'workspace':str(tmp_path),'project_name':'Existing project','sessions':[{'context':str(context)}]}
    profile={'inherit_manager':True}
    assert setup_location(profile,Namespace(),config)==(tmp_path,'Existing project',None)
    claimed={**profile,'resolved_workspace':str(tmp_path),'resolved_project_name':'Frozen name'}
    assert setup_location(claimed,Namespace(),config)==(tmp_path,'Frozen name',None)
    manual={'workspace':str(tmp_path),'project_name':'Manual name','room':{'name':'Unrelated room'}}
    assert setup_location(manual,Namespace())==(tmp_path,'Manual name',None)


def test_migration_preserves_existing_manual_setup(tmp_path):
    path=tmp_path/'old.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE setup_profiles(room TEXT PRIMARY KEY,workspace TEXT NOT NULL,roles TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,updated REAL NOT NULL);
            INSERT INTO setup_profiles VALUES('existing','/user/project','[]',3,1);''')
    store=Store(path)
    with store.tx() as db:
        row=dict(db.execute('SELECT * FROM setup_profiles').fetchone())
    assert row['workspace']=='/user/project' and row['revision']==3
    assert row['inherit_manager']==0 and row['project_name']==''
