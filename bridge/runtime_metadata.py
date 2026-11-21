"""Record the model/effort used by this turn; never substitute new settings for old messages."""
import json
import os
import re
from pathlib import Path

from .model_settings import configured_model


def configured_runtime(session):
    role=session['definition'];harness=role['harness']
    result={'harness':harness,'model':configured_model(role),'think_level':''}
    args=role.get('args',[])
    effort=role.get('reasoning_effort','')
    for index,arg in enumerate(args):
        if arg=='--effort' and index+1<len(args):effort=args[index+1]
        elif arg.startswith('--effort='):effort=arg.split('=',1)[1]
        elif arg.startswith('model_reasoning_effort='):effort=arg.split('=',1)[1].strip('"\'')
    if harness=='codex':
        path=Path(session.get('codex_home') or os.environ.get('CODEX_HOME') or Path.home()/'.codex')/'config.toml'
        try:
            source=path.read_text(encoding='utf-8')
            try:
                import tomllib
                config=tomllib.loads(source)
            except ImportError:
                # Python 3.10 clients only need these top-level string settings.
                config=dict(re.findall(r'^\s*(model|model_reasoning_effort)\s*=\s*"([^"\n]+)"',source.split('\n[')[0],re.M))
            result['model']=result['model'] or config.get('model','')
            effort=effort or config.get('model_reasoning_effort','')
        except (OSError,ValueError):pass
    elif harness=='claude':
        config={}
        for path in (Path.home()/'.claude/settings.json',Path(session.get('workspace','.'))/'.claude/settings.json',Path(session.get('workspace','.'))/'.claude/settings.local.json'):
            try:config.update(json.loads(path.read_text(encoding='utf-8')))
            except (OSError,ValueError):pass
        result['model']=result['model'] or config.get('model','')
        effort=effort or os.environ.get('CLAUDE_CODE_EFFORT_LEVEL','') or config.get('effortLevel','')
    result['think_level']=str(effort or '')[:80]
    result['model']=str(result['model'] or '')[:200]
    return result


def result_runtime(session, objects):
    result=dict(session.get('runtime') or configured_runtime(session))
    for item in objects:
        if not isinstance(item,dict):continue
        envelopes=[item]+[item[k] for k in ('result','init') if isinstance(item.get(k),dict)]
        for envelope in envelopes:
            if isinstance(envelope.get('model'),str):result['model']=envelope['model'][:200]
            for key in ('reasoning_effort','effort','thinking_level'):
                if isinstance(envelope.get(key),str):result['think_level']=envelope[key][:80]
            usage=envelope.get('modelUsage')
            if isinstance(usage,dict) and len(usage)==1:result['model']=next(iter(usage))[:200]
    if session['definition']['harness']=='codex' and re.fullmatch(r'[a-f0-9-]{36}',session.get('native_id','')):
        root=Path(session.get('codex_home') or os.environ.get('CODEX_HOME') or Path.home()/'.codex')/'sessions'
        cached=session.get('rollout_path')
        path=Path(cached) if cached and session['native_id'] in Path(cached).name else None
        if path is None or not path.is_file():
            path=next(root.rglob('*'+session['native_id']+'*.jsonl'),None) if root.is_dir() else None
        if path:
            try:
                # Read only this exact session's latest turn context, without publishing its contents.
                with path.open('rb') as file:
                    file.seek(0,2);size=file.tell();file.seek(max(0,size-4*1024*1024));tail=file.read()
                for line in reversed(tail.splitlines()):
                    try:entry=json.loads(line)
                    except ValueError:continue
                    if entry.get('type')=='turn_context':
                        context=entry.get('payload',{})
                        if isinstance(context.get('model'),str):result['model']=context['model'][:200]
                        if isinstance(context.get('effort'),str):result['think_level']=context['effort'][:80]
                        break
                session['rollout_path']=str(path)
            except OSError:pass
    return result
