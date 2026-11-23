"""Both AGY launch paths must suppress its separate background updater."""
import sys
import threading

import pytest

from bridge.harness_environment import harness_environment
from bridge.model_catalog import discover_models
from bridge import runner


def test_updater_setting_is_scoped_to_managed_antigravity():
    source={'AGY_CLI_DISABLE_AUTO_UPDATE':'0','HTTPS_PROXY':'proxy','CODEX_HOME':'own-home'}
    assert harness_environment(source,harness='antigravity')['AGY_CLI_DISABLE_AUTO_UPDATE']=='true'
    assert harness_environment(source,harness='claude')==source
    assert source['AGY_CLI_DISABLE_AUTO_UPDATE']=='0'


@pytest.mark.parametrize('operation',['models','task'])
def test_background_operations_do_not_start_updater(tmp_path,monkeypatch,operation):
    # A real subprocess mimics the vendor's updater side effect. This catches a
    # launch path that forgets the environment even if the helper is correct.
    script=tmp_path/'models'
    script.write_text('''import json,os,sys
from pathlib import Path
if os.environ.get('AGY_CLI_DISABLE_AUTO_UPDATE')!='true':
    Path('updater-started').touch()
if '--task' in sys.argv:
    prompt=json.load(sys.stdin)
    assert prompt['event']=='user' and prompt['message']['content']=='perform assigned task'
    print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'@owner done','conversation_id':'original'}}))
else:
    print('model-a\\tModel A')
''',encoding='utf-8')
    monkeypatch.setenv('AGY_CLI_DISABLE_AUTO_UPDATE','0')
    session={'id':'worker','name':'worker','native_id':'original','context':str(tmp_path/'session.json'),
             'temp_dir':str(tmp_path),'workspace':str(tmp_path),
             'definition':{'harness':'antigravity','executable':sys.executable}}
    if operation=='models':
        assert discover_models(session)['models']==[{'id':'model-a','label':'Model A'}]
    else:
        monkeypatch.setattr(runner,'build_command',lambda *args:[sys.executable,str(script),'--task'])
        bridge=runner.Bridge.__new__(runner.Bridge)
        bridge.workspace=tmp_path;bridge.stop=threading.Event();bridge.steer_capabilities={}
        bridge.prompt=lambda *args:'perform assigned task'
        bridge.complete_response=lambda s,d,r:r
        job={'cancel':threading.Event()}
        assert bridge.execute(session,{'id':'task','message':{'id':1}},job,tmp_path)=='@owner done'
        assert session['native_id']=='original'
    assert not (tmp_path/'updater-started').exists()
