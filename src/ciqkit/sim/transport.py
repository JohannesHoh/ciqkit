"""The broker's control channel: how a client reaches the one daemon.

Two transports behind one interface, chosen per platform:

* **unix socket** (macOS, Linux) -- the endpoint path IS the socket. Access
  control is the filesystem's: the socket lives in the private 0700 runtime
  dir and is itself 0600. See SECURITY.md.

* **loopback TCP + token** (Windows, whose CPython has no ``AF_UNIX``) -- the
  daemon listens on an ephemeral ``127.0.0.1`` port and writes
  ``{"port", "token", "pid"}`` to the endpoint path, a file in the same private
  runtime directory (on Windows: a protected DACL, this user and SYSTEM only).
  Any local account can *connect* to a loopback port, so a connection is only
  served after its first line proves knowledge of the token -- which only
  someone who can read this user's private directory has. A browser cannot
  supply it either, which matters because a loopback port is reachable from
  web pages (DNS rebinding, blind POSTs): their first line is an HTTP request
  line, never the token, and the connection is dropped before any op runs.

Before it has authenticated, a TCP connection holds none of the broker's
handler slots: it sits in a small pre-auth pool (:meth:`Listener.admit`) that
evicts its oldest member when full, and it has one absolute deadline for the
whole auth line, not a timeout per read. Otherwise any local account could
drip a byte every few seconds down 64 connections and lock the owner out.

The client, for its part, does not hand the token to whatever happens to be
listening on the port an endpoint file names: a broker killed hard leaves that
file behind, and another account could have bound the port since. See
:func:`connect`.

``CIQSIM_TRANSPORT=tcp`` forces the TCP transport anywhere, which is how its
tests run on POSIX CI.

Stdlib only, like :mod:`ciqkit.sim.protocol`.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import threading
import time

from .. import _compat
from . import protocol as proto

AUTH_FIELD = "auth"
_HOST = "127.0.0.1"
# SECURITY.md promises: no token within 10 s -> unauthorized and closed. One
# deadline for the whole line, not a timeout per read.
AUTH_DEADLINE_S = 10.0
# The auth line is ~80 bytes; anything longer is not one.
AUTH_LINE_MAX = 4096
# Connections that have not authenticated yet. Small, because every member is
# a thread; full, it evicts the OLDEST rather than refusing the newest, so
# connections parked here by someone without the token cannot keep the owner's
# client -- which sends its token the moment it connects -- from getting in.
PREAUTH_MAX = 32


def use_tcp() -> bool:
    return (os.environ.get("CIQSIM_TRANSPORT", "").lower() == "tcp"
            or not hasattr(socket, "AF_UNIX"))


def default_endpoint_name() -> str:
    # Not ".sock" on the TCP transport: that file is JSON naming a port, and a
    # name that promises a socket sends whoever finds it looking for one.
    return "ciqsimd.endpoint" if use_tcp() else "ciqsimd.sock"


class Listener:
    """A bound, listening control channel. ``token`` is None for unix sockets,
    where the filesystem already did the access check."""

    def __init__(self, sock: socket.socket, endpoint: str, token: str | None):
        self.sock = sock
        self.endpoint = endpoint
        self.token = token
        self._pending: dict[socket.socket, None] = {}   # insertion-ordered
        self._pending_lock = threading.Lock()

    def accept(self):
        conn, _ = self.sock.accept()
        return conn

    def admit(self, conn: socket.socket) -> None:
        """Enter a fresh TCP connection in the pre-auth pool, evicting the
        oldest member if it is full. Call before :meth:`authenticate`."""
        with self._pending_lock:
            if len(self._pending) >= PREAUTH_MAX:
                oldest = next(iter(self._pending))
                del self._pending[oldest]
                # shutdown, not close: it wakes the owning thread's recv, and
                # that thread still owns (and closes) the socket. Inside the
                # lock, so the owner cannot have closed it yet -- it leaves the
                # pool under this lock before it closes anything.
                try:
                    oldest.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self._pending[conn] = None

    def forget(self, conn: socket.socket) -> None:
        """Drop ``conn`` from the pre-auth pool (its handler never started)."""
        with self._pending_lock:
            self._pending.pop(conn, None)

    def authenticate(self, conn: socket.socket) -> bool:
        """Consume and check the auth line on a fresh connection.

        Always True on a unix socket. On TCP the client's first line must be
        ``{"auth": "<token>"}`` and must arrive within ``AUTH_DEADLINE_S`` of
        the call; anything else -- garbage, an HTTP request, a wrong or
        non-ASCII token, a peer dripping bytes -- is refused, as is a
        connection evicted from the pre-auth pool meanwhile. Compared in
        constant time. ``conn``'s timeout is left at whatever remained: the
        caller sets the next one.
        """
        if self.token is None:
            return True
        try:
            line = _read_auth_line(conn, time.monotonic() + AUTH_DEADLINE_S)
        finally:
            with self._pending_lock:
                evicted = self._pending.pop(conn, True) is True
        if line is None or evicted:
            return False
        try:
            got = proto.loads(line).get(AUTH_FIELD, "")
            # Bytes, not str: compare_digest raises TypeError on a str with
            # any non-ASCII character in it.
            return hmac.compare_digest(got.encode("utf-8"), self.token.encode("ascii"))
        except (ValueError, AttributeError, UnicodeError):
            return False

    def close(self):
        try:
            self.sock.close()
        finally:
            remove_endpoint(self.endpoint)


def _read_auth_line(conn: socket.socket, deadline: float) -> bytes | None:
    """The first line on ``conn``, or None if it is not complete by
    ``deadline`` (monotonic) or runs past ``AUTH_LINE_MAX``.

    One byte per recv, so nothing after the newline is consumed: the client
    may have sent its first request right behind the token, and the handler's
    own reader must start exactly there. ~80 syscalls per connection is cheap.
    The timeout is recomputed before every recv, so a peer trickling bytes
    gets the same total budget as one that sends nothing.
    """
    buf = bytearray()
    while len(buf) < AUTH_LINE_MAX:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            conn.settimeout(remaining)
            b = conn.recv(1)
        except OSError:                 # timeout, reset, or evicted (shutdown)
            return None
        if not b:
            return None
        buf += b
        if b == b"\n":
            return bytes(buf)
    return None


def remove_endpoint(endpoint: str) -> None:
    try:
        os.unlink(endpoint)
    except OSError:
        pass


def bind_unix(path: str) -> Listener:
    """Bind the unix socket 0600 (the caller has checked the directory)."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o177)
    try:
        srv.bind(path)
    finally:
        os.umask(old_umask)
    os.chmod(path, 0o600)
    return Listener(srv, path, None)


def bind_tcp(endpoint: str) -> Listener:
    """Listen on an ephemeral loopback port and publish it with a fresh token.

    The endpoint file is written only after ``listen()``, and atomically, so
    a client that can read it can connect. A new token per daemon means an
    endpoint file left behind by a dead broker grants nothing.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        # Windows: without it another process may bind the same port with
        # SO_REUSEADDR and steal connections meant for us.
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    srv.bind((_HOST, 0))
    srv.listen(64)
    token = secrets.token_hex(32)
    info = {"transport": "tcp", "host": _HOST, "port": srv.getsockname()[1],
            "token": token, "pid": os.getpid()}
    tmp = endpoint + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | getattr(os, "O_BINARY", 0),
                 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(info, fh)
    os.replace(tmp, endpoint)
    return Listener(srv, endpoint, token)


def bind(endpoint: str) -> Listener:
    lst = bind_tcp(endpoint) if use_tcp() else bind_unix(endpoint)
    if lst.token is None:
        lst.sock.listen(64)
    return lst


def _tcp_listeners():
    """``(port, pid, address)`` of every listening TCP socket, or None where
    this platform cannot tell (POSIX, where TCP is a test-only transport)."""
    if not _compat.IS_WIN:
        return None
    from .. import _winapi
    try:
        return _winapi.tcp_listeners()
    except OSError:
        return None


def _owns_port(pid: int, host: str, port: int) -> bool:
    rows = _tcp_listeners()
    if not rows:
        # Unknown, or a table read that failed (never legitimately empty on
        # Windows). Fall back to the liveness check alone rather than lock the
        # owner out of a broker that is fine.
        return True
    return any(p == port and owner == pid and addr == host for p, owner, addr in rows)


def connect(endpoint: str, timeout: float = 5.0) -> socket.socket:
    """Connect and, on TCP, authenticate. Raises FileNotFoundError or
    ConnectionRefusedError when no daemon is there -- the two errors the client
    maps to "not running" -- whichever transport is in use."""
    if not use_tcp():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(endpoint)
        except BaseException:
            s.close()
            raise
        return s
    with open(endpoint) as fh:          # FileNotFoundError: never started
        try:
            info = json.load(fh)
            host, port, pid = info.get("host", _HOST), int(info["port"]), int(info["pid"])
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ConnectionRefusedError(f"unreadable broker endpoint {endpoint}") from None
    # A broker killed hard (SIGKILL, power loss) leaves this file behind, and
    # by now another local account may have bound that port. Sending it the
    # token would gain it nothing against the NEXT broker (tokens are per
    # broker), but it could pose as one and answer with fabricated results.
    # So: the broker that wrote the file must still be alive and, where the
    # OS can say who owns a port, be the one listening there.
    if not _compat.pid_alive(pid) or not _owns_port(pid, host, port):
        raise ConnectionRefusedError(f"stale broker endpoint {endpoint} "
                                     f"(pid {pid} is not listening on port {port})")
    s = socket.create_connection((host, port), timeout=timeout)   # refused: a stale file
    try:
        s.sendall(proto.dumps({AUTH_FIELD: info["token"]}))
    except BaseException:
        s.close()
        raise
    return s
