"""`sim shutdown` followed straight away by another command.

The SHUTDOWN reply used to come back while the broker was still accepting
connections. The next command's SUBMIT landed on the dying broker, its job
vanished with it, and the WAIT -- which autospawned a fresh broker -- came back
"no such job". Seen as the first `sim test` after `sim shutdown` failing.
"""
import os
import threading
import time

from ciqkit._compat import open_lock_file, try_lock
from ciqkit.sim import client as client_mod
from ciqkit.sim import protocol as proto
from ciqkit.sim.broker import Broker
from ciqkit.sim.protocol import Chunk, ChunkKind, Job, Op


def _job():
    return Job(app="a", device="venu3", chunks=[Chunk(ChunkKind.SHOT, {"name": "x"})])


def test_a_stopping_broker_refuses_new_jobs():
    b = Broker()
    assert b.handle({"op": Op.SHUTDOWN})["ok"] is True
    r = b.handle({"op": Op.SUBMIT, "job": _job().to_dict()})
    assert r == {"ok": False, "error": proto.ERR_SHUTTING_DOWN}
    assert b.jobs == {}, "a job was accepted by a broker that is going away"


def _held_lock(sock_path):
    fd = open_lock_file(sock_path + ".lock")
    assert try_lock(fd)
    return fd


def test_shutdown_returns_only_once_the_broker_has_exited(tmp_path, monkeypatch):
    sock = str(tmp_path / "s.sock")
    fd = _held_lock(sock)                  # the "broker" is still alive
    c = client_mod.Client(sock_path=sock, autospawn=False)
    monkeypatch.setattr(c, "_rpc", lambda req, **k: {"ok": True})
    released = []

    def exit_later():
        time.sleep(0.4)
        released.append(time.monotonic())
        os.close(fd)                       # process exit releases the flock
    threading.Thread(target=exit_later).start()

    r = c.shutdown(wait_s=5)
    assert r["ok"] is True and r["stopped"] is True
    assert released and time.monotonic() >= released[0]


def test_shutdown_reports_a_broker_that_did_not_exit(tmp_path, monkeypatch):
    sock = str(tmp_path / "s.sock")
    fd = _held_lock(sock)
    try:
        c = client_mod.Client(sock_path=sock, autospawn=False)
        monkeypatch.setattr(c, "_rpc", lambda req, **k: {"ok": True})
        assert c.shutdown(wait_s=0.3)["stopped"] is False
    finally:
        os.close(fd)


def test_no_lock_file_means_nothing_to_wait_for(tmp_path):
    c = client_mod.Client(sock_path=str(tmp_path / "s.sock"), autospawn=False)
    assert c.wait_gone(timeout_s=0.1) is True


def test_submit_to_a_stopping_broker_waits_and_resubmits(tmp_path, monkeypatch):
    c = client_mod.Client(sock_path=str(tmp_path / "s.sock"), autospawn=False)
    replies = [{"ok": False, "error": proto.ERR_SHUTTING_DOWN},
               {"ok": True, "job_id": "new"}]
    sent, waited = [], []
    monkeypatch.setattr(c, "_rpc", lambda req, **k: (sent.append(req), replies.pop(0))[1])
    monkeypatch.setattr(c, "wait_gone", lambda *a, **k: waited.append(1) or True)
    assert c.submit(_job()) == "new"
    assert len(sent) == 2 and waited == [1]
