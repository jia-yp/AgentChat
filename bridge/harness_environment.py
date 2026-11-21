"""Launch independent harness sessions without a parent desktop task's IPC state."""
import os


DESKTOP_STATE = {
    'CODEX_THREAD_ID', 'CODEX_TURN_ID', 'CODEX_SESSION_ID', 'CODEX_TOOL_CALL_ID',
    'CODEX_WINDOWS_REGISTERED_CORE', 'CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY',
    'CODEX_APP_RUNTIME_CLI_PATH', 'CODEX_APP_TOOLS_PIPE_PATH', 'CODEX_PERMISSION_PROFILE',
    'CODEX_TASK_WORKSPACE_VERIFYING_IDENTITY', 'CODEX_INTERNAL_ORIGINATOR_OVERRIDE',
    'CODEX_SHELL', 'CLAUDECODE',
}


def harness_environment(source=None):
    env=dict(os.environ if source is None else source)
    for key in DESKTOP_STATE:
        env.pop(key,None)
    # Credentials/config homes and user-selected proxy settings are preserved.
    env.pop('AGENTCHAT_JOIN_TOKEN',None)
    return env
