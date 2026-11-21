"""Model configuration shared by setup, adapters and the live bridge."""
MODEL_FLAGS = {'codex': ('--model', '-m'), 'claude': ('--model',), 'antigravity': ('--model', '-m')}


def supports_model(role):
    return role['harness'] in ('codex','claude','antigravity','deepseek') or (
        role['harness']=='command' and any('{model}' in part for part in role.get('command',[])))


def model_arguments(role):
    """Extract model overrides so extra CLI flags cannot silently beat the web setting."""
    flags=MODEL_FLAGS.get(role['harness'],())
    args=role.get('args',[])
    clean=[];model='';index=0
    while index<len(args):
        arg=args[index]
        if arg in flags:
            if index+1>=len(args):raise ValueError(arg+' 缺少模型 ID')
            model=args[index+1];index+=2;continue
        if any(arg.startswith(flag+'=') for flag in flags):
            model=arg.split('=',1)[1]
        elif '-m' in flags and arg.startswith('-m') and len(arg)>2:
            model=arg[2:]
        else:clean.append(arg)
        index+=1
    return clean,model


def configured_model(role):
    return role.get('model') or model_arguments(role)[1] or ''
