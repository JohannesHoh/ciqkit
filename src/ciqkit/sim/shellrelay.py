"""Windows half of simport's monkeydo shell shim: run the SDK's shell.exe for
the simulator's port and die with the batch file that started us.

MonkeyDoDeux ends every shell with Process.destroy(). Through the shim that
process is cmd.exe, and TerminateProcess takes no children with it: the
orphaned shell.exe held MonkeyDoDeux's output pipe open, its reader thread
never saw EOF, and a test run that had printed PASSED never exited. POSIX
has no such problem because the shim there execs the real shell.

Run as a plain file (``python shellrelay.py ARGS``), not ``-m``: whichever
python the shim names may not have ciqkit importable. Stdlib only.
"""
import ctypes
import os
import subprocess
import sys
from ctypes import wintypes as wt

_SYNCHRONIZE = 0x00100000
_INFINITE = 0xFFFFFFFF
_WAIT_OBJECT_0 = 0


def main(argv) -> int:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.WaitForMultipleObjects.restype = wt.DWORD
    k32.WaitForMultipleObjects.argtypes = [wt.DWORD, ctypes.POINTER(wt.HANDLE),
                                           wt.BOOL, wt.DWORD]
    # cmd.exe waits on us, so its pid is still its own here; take the handle
    # before anything slow so a destroy cannot land in between unnoticed.
    parent = k32.OpenProcess(_SYNCHRONIZE, False, os.getppid())
    try:
        # Explicit fds: the shell talks to MonkeyDoDeux over these pipes.
        child = subprocess.Popen([os.environ["CIQ_REAL_SHELL"], *argv],
                                 stdin=0, stdout=1, stderr=2)
    except OSError as e:
        sys.stderr.write(f"shellrelay: {e}\n")
        return 1
    if not parent:
        return child.wait()
    handles = (wt.HANDLE * 2)(parent, int(child._handle))
    if k32.WaitForMultipleObjects(2, handles, False, _INFINITE) == _WAIT_OBJECT_0:
        child.kill()   # cmd.exe was destroyed: take the shell with it
        child.wait()
        return 1
    return child.wait()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
