"""Exercise real Windows descendants, not just the Popen keyword arguments."""
import json
import os
import subprocess
import sys
import time

import pytest

from bridge.adapters import stop_tree
from bridge.processes import background_options


pytestmark=pytest.mark.skipif(os.name!='nt',reason='Windows console inheritance')

PROBE=r'''
import ctypes,json,os,subprocess,sys,time
from pathlib import Path
kernel=ctypes.WinDLL('kernel32',use_last_error=True)
user=ctypes.WinDLL('user32',use_last_error=True)
kernel.GetConsoleWindow.restype=ctypes.c_void_p
user.IsWindowVisible.argtypes=[ctypes.c_void_p]
window=kernel.GetConsoleWindow()
state={'pid':os.getpid(),'console':window,'visible':bool(user.IsWindowVisible(window))}
if sys.argv[1]=='leaf':
    state['input']=sys.stdin.read()
    sys.stderr.write('leaf stderr survives\n')
elif sys.argv[1]=='sleep':
    Path(sys.argv[2]).write_text(json.dumps(state))
    time.sleep(120)
else:
    payload=sys.stdin.read()
    cmd=[os.environ['COMSPEC'],'/d','/c','probe.cmd'] if sys.argv[1]=='cmd' else [sys.executable,__file__,'leaf']
    child=subprocess.run(cmd,input=payload,capture_output=True,text=True,encoding='utf-8',check=True,timeout=15)
    state['child']=json.loads(child.stdout)
    sys.stderr.write(child.stderr)
print(json.dumps(state,ensure_ascii=False))
'''


@pytest.mark.parametrize('via',['direct','cmd'])
def test_console_is_hidden_and_inherited_with_stdio_intact(tmp_path,via):
    script=tmp_path/'probe.py';script.write_text(PROBE,encoding='utf-8')
    (tmp_path/'probe.cmd').write_text('@"'+sys.executable+'" "%~dp0probe.py" leaf\r\n',encoding='utf-8')
    payload='@worker 中文 & | < > %PATH% "quoted"\nnext line'
    result=subprocess.run([sys.executable,str(script),via],input=payload,cwd=tmp_path,
        env={**os.environ,'PYTHONIOENCODING':'utf-8'},capture_output=True,text=True,encoding='utf-8',
        timeout=25,check=True,**background_options())
    state=json.loads(result.stdout)
    assert state['console'] and not state['visible']
    assert state['child']['console']==state['console'] and not state['child']['visible']
    assert state['child']['input']==payload
    assert result.stderr=='leaf stderr survives\n'


def test_background_tree_is_still_cancellable(tmp_path):
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    probe=tmp_path/'probe.py';probe.write_text(PROBE,encoding='utf-8')
    child_state=tmp_path/'child.json'
    parent=tmp_path/'parent.py'
    parent.write_text('import subprocess,sys,time\nsubprocess.Popen([sys.executable,sys.argv[1],"sleep",sys.argv[2]])\ntime.sleep(120)\n')
    process=subprocess.Popen([sys.executable,str(parent),str(probe),str(child_state)],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**background_options())
    handle=None
    try:
        deadline=time.monotonic()+10
        while not child_state.exists() and time.monotonic()<deadline:time.sleep(.02)
        child=json.loads(child_state.read_text())
        assert child['console'] and not child['visible']
        handle=kernel.OpenProcess(0x00100000,False,child['pid']) # SYNCHRONIZE only
        assert handle
        stop_tree(process)
        process.wait(timeout=10)
        assert kernel.WaitForSingleObject(handle,10000)==0
    finally:
        stop_tree(process)
        if handle:kernel.CloseHandle(handle)
