"""Background launches that also keep console descendants out of the desktop."""
import os
import subprocess


def background_options(*, new_session=True):
    if os.name!='nt':
        return {'start_new_session':True} if new_session else {}
    # CREATE_NO_WINDOW leaves the harness with no console to inherit. A CLI
    # it launches without its own hiding flags can then create a visible one.
    # Create the console hidden from the outset instead; cmd, MCP servers and
    # other ordinary descendants inherit it. All stdio remains explicitly piped.
    startup=subprocess.STARTUPINFO()
    startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow=subprocess.SW_HIDE
    # NEW_PROCESS_GROUP is ignored with NEW_CONSOLE. Cancellation uses taskkill
    # /T by PID on Windows, and a separate process group on POSIX.
    return {'startupinfo':startup,'creationflags':subprocess.CREATE_NEW_CONSOLE}
