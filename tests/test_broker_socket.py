"""The broker's control channel: privacy, singleton-ness and the accept loop.

This layer had no tests at all, which is uncomfortable for a channel the module
docstring calls a command channel -- a job submitted here can run a project's
build script and patch its source. `_bind_private` is the check SECURITY.md
points at, and nothing pinned its behaviour.

Two transports (see ciqkit.sim.transport): a unix socket where the platform
has one, and loopback TCP + a per-daemon token where it does not (Windows).
The live tests run over every transport the platform can host -- on POSIX
that includes TCP, forced with CIQSIM_TRANSPORT=tcp, so the Windows channel is
also exercised by the Linux and macOS CI jobs.

No simulator is involved: the broker is started, spoken to, and shut down
again. Only ops that never touch a SimSession are exercised.
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

from ciqkit.sim import broker as broker_mod
from ciqkit.sim import protocol as proto
from ciqkit.sim import transport

def _own_trustees() -> set:
    """This user's SID and SYSTEM, as an SDDL string names them. SDDL writes
    well-known accounts by alias: the built-in Administrator (RID 500, which
    GitHub's Windows runners run as) appears as "LA", not as its SID."""
    from ciqkit import _winapi
    sid = _winapi.user_sid()
    return {sid, "SY"} | ({"LA"} if sid.endswith("-500") else set())


IS_WIN = sys.platform == "win32"
posix_only = pytest.mark.skipif(IS_WIN, reason="unix-socket specifics")
TRANSPORTS = ["tcp"] if not hasattr(socket, "AF_UNIX") else ["unix", "tcp"]


@pytest.fixture(params=TRANSPORTS)
def transport_kind(request, monkeypatch):
    if request.param == "tcp":
        monkeypatch.setenv("CIQSIM_TRANSPORT", "tcp")
    else:
        monkeypatch.delenv("CIQSIM_TRANSPORT", raising=False)
    return request.param


# --- _bind_private -----------------------------------------------------------

@posix_only
def test_binding_refuses_a_directory_other_users_can_reach(tmp_path):
    """The socket can start builds, so it must not sit somewhere shared.

    On Linux the socket file's own mode is historically ignored by connect(),
    which makes the containing directory the only thing actually protecting it.
    """
    d = tmp_path / "loose"
    d.mkdir(mode=0o755)
    with pytest.raises(RuntimeError, match="0700"):
        broker_mod._bind_private(str(d / "s.sock"))


@pytest.mark.skipif(not IS_WIN, reason="Windows ownership check")
def test_binding_refuses_a_directory_someone_else_owns(tmp_path, monkeypatch):
    """On Windows the token in the endpoint file IS the access control, so the
    directory holding it must be ours -- or the token is not a secret."""
    from ciqkit import _winapi
    monkeypatch.setattr(_winapi, "owned_by_me", lambda p: (False, "S-1-5-21-0-0-0-1002"))
    with pytest.raises(RuntimeError, match="refusing to listen"):
        broker_mod._bind_private(str(tmp_path / "d.endpoint"))


@posix_only
def test_a_bound_socket_is_private_to_its_owner(short_runtime_dir, monkeypatch):
    # short_runtime_dir, not tmp_path: pytest tmpdirs nest deep enough to blow
    # past the ~104-byte AF_UNIX path limit on macOS.
    monkeypatch.delenv("CIQSIM_TRANSPORT", raising=False)
    sock_path = os.path.join(short_runtime_dir, "s.sock")
    lst = broker_mod._bind_private(sock_path)
    try:
        mode = os.stat(sock_path).st_mode & 0o777
        assert mode == 0o600, f"socket is {oct(mode)}, not 0600"
    finally:
        lst.close()


def _read_json(path):
    # A with, not json.load(open(...)): on Windows a file still open here
    # cannot be deleted by the broker's shutdown.
    with open(path) as fh:
        return json.load(fh)


def _private_file(path):
    if not IS_WIN:
        return (os.stat(path).st_mode & 0o777) == 0o600
    from ciqkit import _winapi
    sddl = _winapi.dacl_sddl(path)
    trustees = set(re.findall(r"\([^;]*;[^;]*;[^;]*;[^;]*;[^;]*;([^)]*)\)", sddl))
    return trustees <= _own_trustees()


def test_a_tcp_endpoint_carries_a_fresh_token_and_is_private(short_runtime_dir,
                                                             monkeypatch):
    monkeypatch.setenv("CIQSIM_TRANSPORT", "tcp")
    from ciqkit import paths
    paths._ensure_private(short_runtime_dir)
    ep = os.path.join(short_runtime_dir, "a.endpoint")
    first = broker_mod._bind_private(ep)
    try:
        info = _read_json(ep)
        assert info["host"] == "127.0.0.1", "must never listen beyond loopback"
        assert len(info["token"]) >= 32
        assert _private_file(ep), "the token file is readable by other users"
        tok1 = info["token"]
    finally:
        first.close()
    assert not os.path.exists(ep), "a stopped broker left its endpoint behind"
    second = broker_mod._bind_private(ep)
    try:
        assert _read_json(ep)["token"] != tok1, \
            "a token that survives restarts is a token a stale file can replay"
    finally:
        second.close()


# --- daemon singleton --------------------------------------------------------

def test_only_one_daemon_can_hold_the_singleton_lock(tmp_path):
    """Replaces a probe-then-unlink dance that could unlink a LIVE socket.

    A daemon that had bound but not yet listen()ed refuses connections, so a
    second daemon starting in that window read the socket as stale and removed
    it -- ending with two brokers on one simulator.
    """
    sock_path = str(tmp_path / "d.sock")
    fd = broker_mod._acquire_singleton_lock(sock_path)
    assert fd is not None
    try:
        assert broker_mod._acquire_singleton_lock(sock_path) is None
    finally:
        os.close(fd)
    # Released with the fd -- the kernel does that, so nothing goes stale.
    fd2 = broker_mod._acquire_singleton_lock(sock_path)
    assert fd2 is not None
    os.close(fd2)


# --- the live accept loop ----------------------------------------------------

def _start(sock_path):
    th = threading.Thread(target=broker_mod.serve, args=(sock_path,), daemon=True)
    th.start()
    for _ in range(100):
        if os.path.exists(sock_path):
            break
        time.sleep(0.05)
    else:
        pytest.fail("the broker never created its endpoint")
    return th


@pytest.fixture
def running_broker(short_runtime_dir, transport_kind, monkeypatch):
    """A real serve() on a real control channel, torn down after the test."""
    monkeypatch.setattr(broker_mod, "leaselib", None)
    sock_path = os.path.join(short_runtime_dir, "t.sock")
    th = _start(sock_path)
    yield sock_path
    try:
        _rpc(sock_path, {"op": proto.Op.SHUTDOWN})
    except OSError:
        pass
    th.join(timeout=10)


def _rpc(sock_path, req, timeout=10.0):
    s = transport.connect(sock_path, timeout=timeout)
    try:
        s.sendall(proto.dumps(req))
        line = s.makefile("rb").readline()
        return proto.loads(line) if line else None
    finally:
        s.close()


def test_the_broker_answers_over_its_channel(running_broker):
    r = _rpc(running_broker, {"op": proto.Op.PING})
    assert r["ok"] is True and r["pid"] == os.getpid()


def test_a_malformed_request_gets_an_error_not_a_dropped_connection(running_broker):
    """`_handle_conn` used to be `except Exception: pass`, so a bug in it
    produced a daemon that answered nothing and looked merely quiet."""
    s = transport.connect(running_broker, timeout=10)
    try:
        s.sendall(b"this is not json\n")
        line = s.makefile("rb").readline()
        assert line, "the connection was dropped instead of answered"
        assert proto.loads(line)["ok"] is False
    finally:
        s.close()


def test_an_unknown_op_is_reported_rather_than_ignored(running_broker):
    r = _rpc(running_broker, {"op": "wat"})
    assert r["ok"] is False and "wat" in r["error"]


def test_a_second_daemon_on_the_same_socket_exits(running_broker, capsys):
    broker_mod.serve(running_broker)      # returns immediately
    assert "already listening" in capsys.readouterr().err


# --- TCP: the token is the access control -----------------------------------

def _raw_tcp(endpoint):
    info = _read_json(endpoint)
    s = socket.create_connection((info["host"], info["port"]), timeout=10)
    return s, info


@pytest.mark.parametrize("first_line", [
    b'{"auth": "0000000000000000000000000000000000000000000000000000000000000000"}\n',
    b'{"op": "shutdown"}\n',                               # an op, no token at all
    b"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",          # a web page's blind POST
])
def test_an_unauthenticated_tcp_peer_runs_nothing(short_runtime_dir, monkeypatch,
                                                  first_line):
    """Any local account -- and any web page, via the browser -- can reach a
    loopback port. None of them can read the token, so none of them may
    submit, cancel or shut down anything."""
    monkeypatch.setenv("CIQSIM_TRANSPORT", "tcp")
    monkeypatch.setattr(broker_mod, "leaselib", None)
    ep = os.path.join(short_runtime_dir, "auth.sock")
    th = _start(ep)
    try:
        s, _info = _raw_tcp(ep)
        try:
            s.sendall(first_line + proto.dumps({"op": proto.Op.SHUTDOWN}))
            reply = s.makefile("rb").readline()
            assert proto.loads(reply) == {"ok": False, "error": "unauthorized"}
        finally:
            s.close()
        # Still alive and still serving the real client.
        assert _rpc(ep, {"op": proto.Op.PING})["ok"] is True
    finally:
        _rpc(ep, {"op": proto.Op.SHUTDOWN})
        th.join(timeout=10)


def test_a_stale_endpoint_reads_as_not_running(short_runtime_dir, monkeypatch):
    """A broker that died hard leaves its endpoint file. The client must map
    that to "not running" (ConnectionRefused), never hang or misreport."""
    monkeypatch.setenv("CIQSIM_TRANSPORT", "tcp")
    ep = os.path.join(short_runtime_dir, "stale.endpoint")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()                                  # nothing listens there now
    with open(ep, "w") as fh:
        json.dump({"host": "127.0.0.1", "port": port, "token": "x" * 64}, fh)
    with pytest.raises(ConnectionRefusedError):
        transport.connect(ep, timeout=5)
    with pytest.raises(FileNotFoundError):
        transport.connect(ep + ".missing", timeout=5)


def test_connections_beyond_the_cap_are_turned_away_not_queued_forever(
        short_runtime_dir, transport_kind, monkeypatch):
    """One thread per connection, each living as long as its client. Unbounded,
    a client in a reconnect loop could pile threads up until the process died.
    """
    monkeypatch.setattr(broker_mod, "leaselib", None)
    monkeypatch.setattr(broker_mod, "MAX_CONNS", 2)
    monkeypatch.setattr(broker_mod, "_CONN_SLOTS", threading.BoundedSemaphore(2))
    sock_path = os.path.join(short_runtime_dir, "cap.sock")
    th = _start(sock_path)

    held = []
    try:
        for _ in range(2):                      # fill both slots, keep them open
            c = transport.connect(sock_path, timeout=10)
            c.sendall(proto.dumps({"op": proto.Op.PING}))
            assert proto.loads(c.makefile("rb").readline())["ok"] is True
            held.append(c)
        r = _rpc(sock_path, {"op": proto.Op.PING})
        assert r["ok"] is False and "connection slots" in r["error"]
    finally:
        for c in held:
            c.close()
        time.sleep(0.3)
        try:
            _rpc(sock_path, {"op": proto.Op.SHUTDOWN})
        except OSError:
            pass
        th.join(timeout=10)


# --- startup and signals ------------------------------------------------------

@posix_only
def test_serve_refuses_a_loose_directory_before_touching_it(tmp_path):
    """The privacy check used to run only at bind time -- after the lock file
    had been created and a stale socket unlinked in that same directory."""
    d = tmp_path / "loose"
    d.mkdir(mode=0o755)
    with pytest.raises(RuntimeError, match="0700"):
        broker_mod.serve(str(d / "s.sock"))
    assert list(d.iterdir()) == [], "serve() wrote into the directory it refused"


@posix_only
def test_sigterm_stops_the_broker_through_the_orderly_path(short_runtime_dir):
    """SIGTERM used to tear down on the main thread and os._exit on the spot:
    seeds restored under a worker still in the session, every WAIT dropped, the
    socket file left behind. Now it takes the same path as SHUTDOWN."""
    sock_path = os.path.join(short_runtime_dir, "sig.sock")
    env = dict(os.environ, CIQSIM_SOCK=sock_path, XDG_RUNTIME_DIR=short_runtime_dir)
    p = subprocess.Popen([sys.executable, "-m", "ciqkit.sim.broker"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(200):
            if os.path.exists(sock_path):
                break
            time.sleep(0.05)
        else:
            pytest.fail("the broker never created its socket")
        assert _rpc(sock_path, {"op": proto.Op.PING})["ok"] is True
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=15) == 0
        assert b"[ciqsimd] stopped" in p.stderr.read(), \
            "the broker exited without running its orderly shutdown"
        assert not os.path.exists(sock_path), "the socket file was left behind"
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
