"""Optional DeepSeek SDK worker, isolated so bridge can cancel the process tree."""
import json
import sys
from pathlib import Path


def main():
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from deepseek_harness import DeepSeekHarness
    from bridge.adapters import discover
    session=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    role=session['definition']
    if not role.get('dsh_home'):
        raise ValueError('DeepSeek 角色必须指定 dsh_home（例如现有 ~/.dsh 的绝对路径）')
    options={'cwd':session['workspace'],'dsh_home':str(Path(role['dsh_home']).expanduser().resolve()),'profile':role.get('profile','sdk')}
    for key in ('model','provider','reasoning_effort','dsh_bin'):
        if role.get(key):options[key]=role[key]
    if 'dsh_bin' not in options:
        installed=discover()['deepseek'].get('desktop_cli')
        if installed:options['dsh_bin']=installed
    with DeepSeekHarness(**options) as harness:
        handle=harness.resume_session(session['native_id']) if session.get('native_id') else harness.start_session(session['requested_id'])
        handle.get_working_directory()
        print(json.dumps({'session_id':handle.id}),flush=True)
        result=handle.run(sys.stdin.read())
        if result.finish_reason not in ('completed',None):
            reasons=[event.get('data',{}).get('reason') for event in result.events if event.get('type')=='turn/end']
            error=result.final_response or json.dumps(reasons[-1:] or [result.finish_reason],ensure_ascii=False)
            print(json.dumps({'session_id':result.session_id,'response':'','error':'DeepSeek: '+error[:3500]},ensure_ascii=False))
            return 1
        print(json.dumps({'session_id':result.session_id,'response':result.final_response},ensure_ascii=False))
    return 0


if __name__=='__main__':
    sys.exit(main())
