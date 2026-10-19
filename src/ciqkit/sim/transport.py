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

from . import protocol as proto

AUTH_FIELD = "auth"
_HOST = "127.0.0.1"


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

    def accept(self):
        conn, _ = self.sock.accept()
        return conn

    def authenticate(self, reader) -> bool:
        """Consume and check the auth line on a fresh connection.

        Always True on a unix socket. On TCP the client's first line must be
        ``{"auth": "<token>"}``; anything else -- garbage, an HTTP request, a
        wrong token -- is refused. Compared in constant time.
        """
        if self.token is None:
            return True
        line = reader.readline(4096)
        try:
            got = proto.loads(line).get(AUTH_FIELD, "")
        except (ValueError, AttributeError, UnicodeDecodeError):
            return False
        return isinstance(got, str) and hmac.compare_digest(got, self.token)

    def close(self):
        try:
            self.sock.close()
        finally:
            remove_endpoint(self.endpoint)


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
        except ValueError:
            raise ConnectionRefusedError(f"unreadable broker endpoint {endpoint}") from None
    s = socket.create_connection((info.get("host", _HOST), int(info["port"])),
                                 timeout=timeout)   # refused: a stale file
    try:
        s.sendall(proto.dumps({AUTH_FIELD: info["token"]}))
    except BaseException:
        s.close()
        raise
    return s
