"""The handful of Win32 calls ciqkit needs, through ctypes.

Stdlib only, like the rest of the broker: pywin32 would be a heavy dependency
for six functions, and the base install has none at all on purpose. Import this
only on Windows -- it binds ``ctypes.windll`` at import time.

Why these and not a subprocess each:

* process and port listing go through Toolhelp32 and ``GetExtendedTcpTable``
  rather than ``tasklist`` / ``netstat``, whose output is LOCALISED. On a German
  Windows ``netstat`` says ``ABHÖREN`` where the parser expects ``LISTENING``,
  so a text parser silently finds no simulator and every push is reported as
  "Unable to connect" -- the exact failure :mod:`ciqkit.sim.simport` exists to
  explain.
* pid liveness goes through ``OpenProcess`` because the POSIX idiom,
  ``os.kill(pid, 0)``, is not a probe on Windows: 0 is CTRL_C_EVENT there.
  It reports dead pids as alive and sends Ctrl+C to the caller's own console
  (see :func:`ciqkit._compat.pid_alive`).
* private directories are expressed as a protected DACL granting only the
  current user (and SYSTEM), which is what a POSIX 0700 means.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import socket

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)

ERROR_ACCESS_DENIED = 5
ERROR_INSUFFICIENT_BUFFER = 122
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x102
TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE_VALUE = wt.HANDLE(-1).value

# ── function prototypes ──────────────────────────────────────────────────────
# Declared, not left to ctypes' int default: HANDLEs and pointers are 64-bit,
# and an undeclared restype truncates them to 32 bits on x64.
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
kernel32.CloseHandle.argtypes = (wt.HANDLE,)
kernel32.WaitForSingleObject.restype = wt.DWORD
kernel32.WaitForSingleObject.argtypes = (wt.HANDLE, wt.DWORD)
kernel32.GetCurrentProcess.restype = wt.HANDLE
kernel32.LocalFree.restype = wt.HLOCAL
kernel32.LocalFree.argtypes = (wt.HLOCAL,)
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = (wt.DWORD, wt.DWORD)
kernel32.ProcessIdToSessionId.argtypes = (wt.DWORD, ctypes.POINTER(wt.DWORD))


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", wt.LONG),
                ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260)]


kernel32.Process32FirstW.argtypes = (wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
kernel32.Process32NextW.argtypes = (wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W))

advapi32.OpenProcessToken.argtypes = (wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE))
advapi32.GetTokenInformation.argtypes = (wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                         wt.DWORD, ctypes.POINTER(wt.DWORD))
advapi32.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR))
advapi32.ConvertStringSidToSidW.argtypes = (wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))
advapi32.CheckTokenMembership.argtypes = (wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(wt.BOOL))
advapi32.GetNamedSecurityInfoW.restype = wt.DWORD
advapi32.GetNamedSecurityInfoW.argtypes = (
    wt.LPCWSTR, ctypes.c_int, wt.DWORD, ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p))
advapi32.SetNamedSecurityInfoW.restype = wt.DWORD
advapi32.SetNamedSecurityInfoW.argtypes = (
    wt.LPWSTR, ctypes.c_int, wt.DWORD, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_void_p, ctypes.c_void_p)
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
    wt.LPCWSTR, wt.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.ULONG))
advapi32.GetSecurityDescriptorDacl.argtypes = (
    ctypes.c_void_p, ctypes.POINTER(wt.BOOL), ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(wt.BOOL))

iphlpapi.GetExtendedTcpTable.restype = wt.DWORD
iphlpapi.GetExtendedTcpTable.argtypes = (ctypes.c_void_p, ctypes.POINTER(wt.DWORD),
                                         wt.BOOL, wt.ULONG, ctypes.c_int, wt.ULONG)


# ── processes ────────────────────────────────────────────────────────────────
def pid_alive(pid: int) -> bool:
    """True while ``pid`` names a running process. Never signals it."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
    if not h:
        # Exists but belongs to someone we may not open (another user, a
        # protected process) -- the POSIX EPERM case, which also means alive.
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        # A handle to an exited process stays valid while anyone holds it, so
        # "could open it" is not "it is running"; a signalled handle is dead.
        return kernel32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(h)


def processes() -> list[tuple[int, int, str]]:
    """Every process as ``(pid, parent pid, exe name)``."""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return []
    out = []
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out.append((e.th32ProcessID, e.th32ParentProcessID, e.szExeFile))
            ok = kernel32.Process32NextW(snap, ctypes.byref(e))
    finally:
        kernel32.CloseHandle(snap)
    return out


_ntdll = ctypes.WinDLL("ntdll")
_ntdll.NtQueryInformationProcess.restype = ctypes.c_long
_ntdll.NtQueryInformationProcess.argtypes = (wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wt.ULONG, ctypes.POINTER(wt.ULONG))
kernel32.TerminateProcess.argtypes = (wt.HANDLE, wt.UINT)
_ProcessCommandLineInformation = 60   # Windows 8.1+
PROCESS_TERMINATE = 0x0001


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wt.USHORT), ("MaximumLength", wt.USHORT),
                ("Buffer", ctypes.c_void_p)]


def command_line(pid: int) -> str | None:
    """The full command line of ``pid``, or None if it cannot be read.

    What ``pkill -f`` matches on. Read in-process: ``wmic`` -- what the old
    code shelled out to -- is removed from current Windows 11 builds, and
    PowerShell's CIM costs a second per call.
    """
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        need = wt.ULONG(0)
        _ntdll.NtQueryInformationProcess(h, _ProcessCommandLineInformation, None, 0,
                                         ctypes.byref(need))
        if not need.value:
            return None
        buf = ctypes.create_string_buffer(need.value)
        if _ntdll.NtQueryInformationProcess(h, _ProcessCommandLineInformation, buf,
                                            need, ctypes.byref(need)) != 0:
            return None
        us = _UNICODE_STRING.from_buffer(buf)
        if not us.Buffer or not us.Length:
            return ""
        return ctypes.wstring_at(us.Buffer, us.Length // 2)
    finally:
        kernel32.CloseHandle(h)


def terminate(pid: int) -> bool:
    h = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not h:
        return False
    try:
        return bool(kernel32.TerminateProcess(h, 1))
    finally:
        kernel32.CloseHandle(h)


def session_id(pid: int | None = None) -> int | None:
    """Terminal-services session of ``pid`` (default: this process)."""
    out = wt.DWORD()
    if not kernel32.ProcessIdToSessionId(os.getpid() if pid is None else pid,
                                         ctypes.byref(out)):
        return None
    return out.value


def session_locked() -> bool | None:
    """True while this logon session is locked, None if that cannot be told.

    Asked of Terminal Services (WTSSessionInfoEx -> SessionFlags), because the
    obvious check does not work: since Windows 10 the lock screen is LockApp on
    the *Default* desktop, so OpenInputDesktop still answers "Default" while
    the session is locked. Locked, PrintWindow captures keep working but every
    synthesized click and key is discarded.
    """
    try:
        wts = ctypes.WinDLL("wtsapi32")
        sid = session_id()
        if sid is None:
            return None
        buf = ctypes.c_void_p()
        size = wt.DWORD()
        if not wts.WTSQuerySessionInformationW(None, sid, 25,   # WTSSessionInfoEx
                                               ctypes.byref(buf), ctypes.byref(size)):
            return None
        try:
            if size.value < 20:
                return None
            # WTSINFOEXW: DWORD Level, then the WTSINFOEX_LEVEL union -- which
            # holds LARGE_INTEGERs, so it is 8-byte aligned and starts at
            # offset 8, not 4. WTSINFOEX_LEVEL1_W: ULONG SessionId,
            # WTS_CONNECTSTATE_CLASS SessionState, LONG SessionFlags. Reading
            # dword 3 instead of 4 gets SessionState, which is 0 (WTSActive)
            # for every logged-in session -- "locked", always.
            dwords = ctypes.cast(buf, ctypes.POINTER(ctypes.c_long))
            if dwords[0] != 1:      # Level
                return None
            flags = dwords[4]
        finally:
            wts.WTSFreeMemory(buf)
        if flags == 0:      # WTS_SESSIONSTATE_LOCK
            return True
        if flags == 1:      # WTS_SESSIONSTATE_UNLOCK
            return False
        return None
    except (OSError, AttributeError):
        return None


def pids_named(exe: str, same_session: bool = True) -> list[int]:
    """PIDs whose image name is ``exe`` (case-insensitive).

    ``same_session`` keeps it to the caller's logon session -- the Windows
    analogue of ``pgrep -u $UID``: another user's simulator on the same machine
    is not ours to drive, and certainly not ours to kill.
    """
    exe = exe.lower()
    mine = session_id() if same_session else None
    out = []
    for pid, _ppid, name in processes():
        if name.lower() != exe:
            continue
        if mine is not None and session_id(pid) != mine:
            continue
        out.append(pid)
    return out


# ── TCP listeners ────────────────────────────────────────────────────────────
_TCP_TABLE_OWNER_PID_LISTENER = 3


class _ROW4(ctypes.Structure):
    _fields_ = [("state", wt.DWORD), ("laddr", wt.DWORD), ("lport", wt.DWORD),
                ("raddr", wt.DWORD), ("rport", wt.DWORD), ("pid", wt.DWORD)]


class _ROW6(ctypes.Structure):
    _fields_ = [("laddr", ctypes.c_ubyte * 16), ("lscope", wt.DWORD), ("lport", wt.DWORD),
                ("raddr", ctypes.c_ubyte * 16), ("rscope", wt.DWORD), ("rport", wt.DWORD),
                ("state", wt.DWORD), ("pid", wt.DWORD)]


def _tcp_table(family: int, row_type) -> list[tuple[int, int, str]]:
    size = wt.DWORD(0)
    iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, family,
                                 _TCP_TABLE_OWNER_PID_LISTENER, 0)
    for _ in range(4):   # the table can grow between the size probe and the read
        buf = ctypes.create_string_buffer(size.value + 4096)
        size = wt.DWORD(len(buf))
        rc = iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, family,
                                          _TCP_TABLE_OWNER_PID_LISTENER, 0)
        if rc == 0:
            break
        if rc != ERROR_INSUFFICIENT_BUFFER:
            return []
    else:
        return []
    n = ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0]
    rows = ctypes.cast(ctypes.addressof(buf) + ctypes.sizeof(wt.DWORD),
                       ctypes.POINTER(row_type * n)).contents
    return [(socket.ntohs(r.lport & 0xFFFF), r.pid, _addr(family, r.laddr)) for r in rows]


def _addr(family: int, laddr) -> str:
    raw = laddr.to_bytes(4, "little") if family == socket.AF_INET else bytes(laddr)
    return socket.inet_ntop(family, raw)


def tcp_listeners() -> list[tuple[int, int, str]]:
    """``(port, pid, local address)`` for every listening TCP socket, IPv4
    and IPv6. The address matters: Windows lets a 127.0.0.1 bind sit on a
    port another process holds on 0.0.0.0, and the specific one wins."""
    return _tcp_table(socket.AF_INET, _ROW4) + _tcp_table(socket.AF_INET6, _ROW6)


# ── security: "0700" ─────────────────────────────────────────────────────────
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x1
_DACL_SECURITY_INFORMATION = 0x4
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_TOKEN_QUERY = 0x8
_TOKEN_USER = 1
_ADMINISTRATORS = "S-1-5-32-544"


def _sid_str(psid) -> str:
    s = wt.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(psid, ctypes.byref(s)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return s.value
    finally:
        kernel32.LocalFree(ctypes.cast(s, ctypes.c_void_p))


_USER_SID = None


def user_sid() -> str:
    """String SID of the user this process runs as, e.g. ``S-1-5-21-...``."""
    global _USER_SID
    if _USER_SID:
        return _USER_SID
    tok = wt.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), _TOKEN_QUERY,
                                     ctypes.byref(tok)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        need = wt.DWORD()
        advapi32.GetTokenInformation(tok, _TOKEN_USER, None, 0, ctypes.byref(need))
        buf = ctypes.create_string_buffer(need.value)
        if not advapi32.GetTokenInformation(tok, _TOKEN_USER, buf, need, ctypes.byref(need)):
            raise ctypes.WinError(ctypes.get_last_error())
        # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first field is PSID.
        psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        _USER_SID = _sid_str(psid)
        return _USER_SID
    finally:
        kernel32.CloseHandle(tok)


def _in_administrators() -> bool:
    psid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(_ADMINISTRATORS, ctypes.byref(psid)):
        return False
    try:
        member = wt.BOOL()
        if not advapi32.CheckTokenMembership(None, psid, ctypes.byref(member)):
            return False
        return bool(member.value)
    finally:
        kernel32.LocalFree(psid)


def owner_sid(path: str) -> str:
    owner = ctypes.c_void_p()
    sd = ctypes.c_void_p()
    rc = advapi32.GetNamedSecurityInfoW(path, _SE_FILE_OBJECT, _OWNER_SECURITY_INFORMATION,
                                        ctypes.byref(owner), None, None, None,
                                        ctypes.byref(sd))
    if rc:
        raise ctypes.WinError(rc)
    try:
        return _sid_str(owner)
    finally:
        kernel32.LocalFree(sd)


def owned_by_me(path: str) -> tuple[bool, str]:
    """(is ``path`` ours, its owner SID).

    An elevated process creates files owned by BUILTIN\\Administrators rather
    than by the user, so a directory made from an admin prompt is still ours
    when we are, at that moment, a member of that group. Anything else is
    someone else's.
    """
    owner = owner_sid(path)
    if owner == user_sid():
        return True, owner
    return (owner == _ADMINISTRATORS and _in_administrators()), owner


advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (
    ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.POINTER(wt.LPWSTR), ctypes.POINTER(wt.ULONG))


def dacl_sddl(path: str) -> str:
    """``path``'s DACL in SDDL, e.g. ``D:P(A;OICI;FA;;;S-1-5-21-...)(A;OICI;FA;;;SY)``.
    For tests and diagnostics: the way to SEE what make_private did."""
    sd = ctypes.c_void_p()
    rc = advapi32.GetNamedSecurityInfoW(path, _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION,
                                        None, None, None, None, ctypes.byref(sd))
    if rc:
        raise ctypes.WinError(rc)
    try:
        s = wt.LPWSTR()
        if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                sd, 1, _DACL_SECURITY_INFORMATION, ctypes.byref(s), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return s.value
        finally:
            kernel32.LocalFree(ctypes.cast(s, ctypes.c_void_p))
    finally:
        kernel32.LocalFree(sd)


def make_private(path: str) -> None:
    """Give ``path`` a protected DACL: full control for this user and SYSTEM
    only, inherited by everything created inside it. The Windows meaning of
    ``chmod 0700`` -- and, being protected, it ignores whatever a permissive
    parent would otherwise have handed down."""
    set_dacl(path, f"D:P(A;OICI;FA;;;{user_sid()})(A;OICI;FA;;;SY)")


def set_dacl(path: str, sddl: str) -> None:
    """Replace ``path``'s DACL with the one in ``sddl`` (a ``D:`` string).
    A ``D:P`` DACL is protected: nothing is inherited from the parent."""
    sd = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(sd), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present, defaulted = wt.BOOL(), wt.BOOL()
        dacl = ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorDacl(sd, ctypes.byref(present),
                                                  ctypes.byref(dacl), ctypes.byref(defaulted)):
            raise ctypes.WinError(ctypes.get_last_error())
        flags = _DACL_SECURITY_INFORMATION
        if sddl.startswith("D:P"):
            flags |= _PROTECTED_DACL_SECURITY_INFORMATION
        else:
            flags |= 0x20000000   # UNPROTECTED_DACL_SECURITY_INFORMATION
        rc = advapi32.SetNamedSecurityInfoW(path, _SE_FILE_OBJECT, flags,
                                            None, None, dacl, None)
        if rc:
            raise ctypes.WinError(rc)
    finally:
        kernel32.LocalFree(sd)
