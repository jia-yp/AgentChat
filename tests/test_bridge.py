import json
from pathlib import Path

import pytest

from bridge.adapters import build_command,parse_result
from bridge.cli import validate_roles


def test_roles_are_required_and_unique():
    with pytest.raises(ValueError):validate_roles([])
    with pytest.raises(ValueError):validate_roles([{'name':'a','harness':'command','command':['echo'],'role':''}])
    role={'name':'worker','harness':'command','command':['echo'],'role':'own task'}
    with pytest.raises(ValueError):validate_roles([role,role])


def test_chinese_names_and_normalized_duplicates():
    role={'name':'小克','harness':'command','command':['echo'],'role':'审查'}
    assert validate_roles([role])[0]['name']=='小克'
    for name in ('所有人','管理员','../小克','小 克','1小克'):
        with pytest.raises(ValueError):validate_roles([{**role,'name':name}])
    with pytest.raises(ValueError):validate_roles([{**role,'name':'Worker'},{**role,'name':'worker'}])


def test_explicit_codex_resume(tmp_path):
    session={'definition':{'harness':'codex','executable':'codex'},'native_id':'exact-session-id','name':'worker'}
    command=build_command(session,tmp_path,tmp_path)
    assert 'exact-session-id' in command and '--last' not in command
    assert 'danger-full-access' not in command
    assert command[-1]=='-'


def test_antigravity_result_and_identity(tmp_path):
    session={'definition':{'harness':'antigravity'}}
    result=parse_result(session,tmp_path,json.dumps({'event':'result','result':{'status':'SUCCESS','response':'@admin done','conversation_id':'native-1'}}))
    assert result=='@admin done' and session['native_id']=='native-1'


def test_claude_json_error_is_not_success(tmp_path):
    with pytest.raises(RuntimeError):parse_result({'definition':{'harness':'claude'}},tmp_path,json.dumps({'type':'result','is_error':True,'result':'not authenticated'}))


def test_custom_argv_preserves_metacharacters(tmp_path):
    session={'definition':{'harness':'command','command':['tool','{workspace}','{session_id}']},'id':'group-id','native_id':'unique','name':'worker'}
    command=build_command(session,tmp_path,Path('workspace & special'))
    assert command==['tool','workspace & special','unique']


def test_atomic_status_write_recovers_from_transient_windows_reader(tmp_path,monkeypatch):
    from bridge import common
    target=tmp_path/'status.json'
    common.write_json(target,{'running':True})
    original=common.os.replace
    attempts=[]
    def reader_busy_once(source,destination):
        attempts.append(1)
        if len(attempts)==1:raise PermissionError('sharing violation')
        return original(source,destination)
    monkeypatch.setattr(common.os,'replace',reader_busy_once)
    common.write_json(target,{'stopped':True})
    assert common.read_json(target)=={'stopped':True}
    assert list(tmp_path.iterdir())==[target]


def test_client_preserves_agent_url_prefix():
    from bridge.common import Client
    import io
    client=Client('https://chat.example.test/agent-connect/','test-token')
    requests=[]
    class Opener:
        def open(self,request,timeout):
            requests.append(request)
            return io.BytesIO(b'{"ok":true}')
    client.opener=Opener()
    assert client.call('/api/bridge/heartbeat',{})=={'ok':True}
    assert requests[0].full_url=='https://chat.example.test/agent-connect/api/bridge/heartbeat'
    assert requests[0].get_header('Authorization')=='Bearer test-token'


def test_child_harness_does_not_inherit_desktop_task_ipc():
    from bridge.harness_environment import harness_environment
    source={'CODEX_WINDOWS_REGISTERED_CORE':'1','CODEX_APP_TOOLS_PIPE_PATH':'parent-pipe','CODEX_PERMISSION_PROFILE':':danger-full-access','CODEX_HOME':'own-home','CODEX_THREAD_ID':'parent-thread','HTTPS_PROXY':'proxy','CLAUDECODE':'1'}
    assert harness_environment(source)=={'CODEX_HOME':'own-home','HTTPS_PROXY':'proxy'}
    assert source['CODEX_WINDOWS_REGISTERED_CORE']=='1'


def test_final_round_prompt_contains_budget_and_stop_rule(tmp_path):
    from bridge.runner import Bridge
    bridge=Bridge.__new__(Bridge);bridge.workspace=tmp_path;bridge.config={'room':{'id':'room'}}
    class Client:
        def call(self,path):return [] if '/messages?' in path else {'room':{'name':'测试'},'members':[]}
    bridge.client=Client()
    session={'name':'小龙','definition':{'role':'开发'},'temp_dir':str(tmp_path),'context':str(tmp_path/'session.json')}
    prompt=bridge.prompt(session,{'message':{'id':1,'sender_name':'owner','body':'继续实现','targets':['小龙']},'round_budget':{'current':3,'max':3,'remaining':1,'last':True}})
    assert '第 3/3 轮' in prompt and '剩余 1 轮（包含本轮）' in prompt
    assert prompt.index('不再继续工作')>prompt.index('继续实现')
    assert '不执行新的工具操作' in prompt and '最后的决定与总结' in prompt


def test_result_metadata_uses_reported_model(tmp_path):
    session={'definition':{'harness':'claude','model':'opus'},'runtime':{'harness':'claude','model':'opus','think_level':'high'}}
    parse_result(session,tmp_path,json.dumps({'type':'result','session_id':'native','result':'@admin done','modelUsage':{'claude-opus-5-5':{}}}))
    assert session['runtime']=={'harness':'claude','model':'claude-opus-5-5','think_level':'high'}


@pytest.mark.parametrize('harness,payload',[
    ('codex',{'type':'turn.failed','error':{'message':'You have reached your usage limit'}}),
    ('claude',{'type':'result','is_error':True,'result':"You've hit your limit · resets at 5pm"}),
    ('antigravity',{'event':'result','result':{'status':'ERROR','error':'RESOURCE_EXHAUSTED'}}),
    ('command',{'response':'','error':'HTTP 429 Too many requests'}),
])
def test_terminal_rate_limit_errors_are_classified(tmp_path,harness,payload):
    from bridge.harness_errors import HarnessFailure
    with pytest.raises(HarnessFailure) as caught:parse_result({'definition':{'harness':harness}},tmp_path,json.dumps(payload))
    assert caught.value.kind=='rate_limit'


def test_successful_response_about_rate_limits_is_not_an_error(tmp_path):
    assert 'rate limit' in parse_result({'definition':{'harness':'command'}},tmp_path,json.dumps({'response':'@admin I fixed the rate limit test'}))


def test_codex_partial_message_does_not_hide_terminal_rate_limit(tmp_path):
    from bridge.harness_errors import HarnessFailure
    (tmp_path/'last-response.txt').write_text('@admin partial progress',encoding='utf-8')
    output='\n'.join(json.dumps(x) for x in [{'type':'item.completed','item':{'type':'agent_message','text':'partial'}},{'type':'turn.failed','error':{'message':'Rate limit reached'}}])
    with pytest.raises(HarnessFailure) as error:parse_result({'definition':{'harness':'codex'}},tmp_path,output)
    assert error.value.kind=='rate_limit'


def test_antigravity_new_session_reuses_group_project(tmp_path):
    session={'definition':{'harness':'antigravity','executable':'agy'},'project_id':'project-for-manager'}
    command=build_command(session,tmp_path,tmp_path)
    assert command[command.index('--project')+1]=='project-for-manager' and '--new-project' not in command
