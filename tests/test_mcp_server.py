"""The MCP surface, exercised without a simulator.

This is the interface a coding agent drives, so its framing, schemas and
argument validation matter as much as the capture itself. Everything here runs
in-process against the JSON-RPC handler; the capture path is covered by
`test_scheduler.py` and by driving a real simulator by hand.
"""
import json
import threading

import pytest

from ciqkit.sim import mcp_server


@pytest.fixture
def rpc(capsys):
    """Send one JSON-RPC message and return the parsed reply (None if a
    notification produced no output)."""
    def send(msg):
        capsys.readouterr()
        t = mcp_server._handle(msg)
        if t is not None:      # a tools/call answers from its worker thread
            t.join(30)
        out = capsys.readouterr().out.strip()
        return json.loads(out) if out else None
    return send


def _tool(rpc, name, args):
    r = rpc({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
             "params": {"name": name, "arguments": args}})
    res = r["result"]
    return res.get("isError", False), res["content"][0]["text"]


# --- protocol framing --------------------------------------------------------

def test_initialize_reports_a_protocol_version_and_server_name(rpc):
    r = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"}}})
    assert r["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION
    assert r["result"]["serverInfo"]["name"]
    assert "tools" in r["result"]["capabilities"]


def test_initialized_notification_gets_no_reply(rpc):
    """A notification has no id; replying to one corrupts the stream."""
    assert rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_ping_is_answered(rpc):
    assert rpc({"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"] == {}


def test_an_unknown_method_returns_method_not_found(rpc):
    r = rpc({"jsonrpc": "2.0", "id": 3, "method": "nope/nope"})
    assert r["error"]["code"] == -32601


def test_an_unknown_method_notification_gets_no_reply(rpc):
    assert rpc({"jsonrpc": "2.0", "method": "nope/nope"}) is None


# --- tool schemas ------------------------------------------------------------

def test_every_advertised_tool_is_dispatchable(rpc, tmp_path, monkeypatch):
    """A tool in tools/list that the dispatcher does not know is a dead entry
    an agent will call and get an error from.

    Runs from an empty directory on purpose: `project` defaults to the working
    directory, and calling these from inside a real Connect IQ project would
    start builds.
    """
    monkeypatch.chdir(tmp_path)
    names = [t["name"] for t in rpc(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/list"})["result"]["tools"]]
    assert names
    for name in names:
        _is_err, text = _tool(rpc, name, {})
        assert "unknown tool" not in text, f"{name} is advertised but not handled"


def test_looking_at_the_queue_does_not_start_a_broker(rpc, monkeypatch,
                                                      short_runtime_dir):
    """An agent that merely checks status must not leave a daemon behind that
    then idles for ten minutes."""
    # The suite-wide runtime dir is empty, so there is no socket to connect to:
    # with autospawn these would really start a daemon.
    def boom(*_a, **_k):
        raise AssertionError("this tool must not spawn a broker")
    monkeypatch.setattr("ciqkit.sim.client.spawn_daemon", boom)
    for name, args in (("ciqsim_status", {}),
                       ("ciqsim_cancel", {"job_id": "x"}),
                       ("ciqsim_bump", {"job_id": "x"})):
        is_err, text = _tool(rpc, name, args)
        assert not is_err, f"{name} raised instead of reporting the fact"
        assert json.loads(text)["running"] is False
        assert "starts automatically" in text


def test_record_refuses_a_seed_with_a_prebuilt_prg(rpc, tmp_path, monkeypatch):
    """Seeding patches source for the *build*; `prg` skips the build, so the
    recording would show the UNSEEDED app and be reported as verified."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "manifest.xml").write_text("<iq:manifest/>")
    is_err, text = _tool(rpc, "ciqsim_record",
                         {"name": "clip", "prg": "bin/App.prg",
                          "seed": {"SEED_DEMO": "true"}})
    assert is_err
    assert "UNSEEDED" in text


def test_tool_schemas_are_well_formed(rpc):
    tools = rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})["result"]["tools"]
    for t in tools:
        assert t["description"].strip(), f"{t['name']} has no description"
        schema = t["inputSchema"]
        assert schema["type"] == "object"
        for req in schema.get("required", []):
            assert req in schema["properties"], \
                f"{t['name']} requires '{req}' but does not declare it"


def test_project_is_optional_everywhere_and_defaults_to_cwd(rpc):
    """Requiring `project` would force an agent to guess a path it may not know."""
    tools = rpc({"jsonrpc": "2.0", "id": 6, "method": "tools/list"})["result"]["tools"]
    for t in tools:
        assert "project" not in t["inputSchema"].get("required", [])


def test_an_unknown_tool_is_an_error_not_a_crash(rpc):
    is_err, text = _tool(rpc, "ciqsim_nonexistent", {})
    assert is_err and "unknown tool" in text


# --- argument validation (the part that runs before any simulator) -----------

def test_a_non_project_directory_is_rejected_with_a_clear_reason(rpc, tmp_path):
    is_err, text = _tool(rpc, "ciqsim_shot",
                         {"project": str(tmp_path), "name": "x"})
    assert is_err
    assert "manifest.xml" in text


def test_a_missing_directory_is_rejected(rpc, tmp_path):
    is_err, text = _tool(rpc, "ciqsim_shot",
                         {"project": str(tmp_path / "nope"), "name": "x"})
    assert is_err
    assert "does not exist" in text


def test_project_paths_are_expanded_and_absolute(tmp_path, monkeypatch):
    (tmp_path / "manifest.xml").write_text("<iq:manifest/>")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))   # what Windows expands ~ to
    assert mcp_server._project({"project": "~"}) == str(tmp_path.resolve())


def test_project_defaults_to_the_working_directory(tmp_path, monkeypatch):
    (tmp_path / "manifest.xml").write_text("<iq:manifest/>")
    monkeypatch.chdir(tmp_path)
    assert mcp_server._project({}) == str(tmp_path.resolve())


def test_the_job_label_comes_from_the_project_directory_name(tmp_path):
    assert mcp_server._name_of(str(tmp_path / "my-watch-app")) == "my-watch-app"


def test_record_exposes_playback_native_and_force(rpc):
    """An agent can only pass what the schema advertises: playback_ms (pacing),
    native (unframed), and force (the stale-warm-reload escape hatch) must all
    be reachable from the tool, not just the CLI."""
    tools = rpc({"jsonrpc": "2.0", "id": 7,
                 "method": "tools/list"})["result"]["tools"]
    rec = next(t for t in tools if t["name"] == "ciqsim_record")
    props = rec["inputSchema"]["properties"]
    for name in ("playback_ms", "native", "force"):
        assert name in props, f"ciqsim_record schema is missing {name}"
        assert props[name].get("description") or name == "playback_ms"
    assert "warm" in props["force"]["description"]


def test_record_wires_force_native_and_playback_into_the_job(
        tmp_path, monkeypatch):
    """Schema entries that the dispatcher drops are silent no-ops -- the worst
    kind: the agent believes it forced a cold reload and got the stale build."""
    (tmp_path / "manifest.xml").write_text("<iq:manifest/>")
    (tmp_path / "bin").mkdir()
    captured = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def submit_wait(self, job, timeout_s=0):
            captured["job"] = job
            return {"ok": True, "job": {"id": "j", "results": []}}

    monkeypatch.setattr(mcp_server, "Client", FakeClient)
    mcp_server._call_tool("ciqsim_record",
                          {"project": str(tmp_path), "name": "clip",
                           "prg": "bin/App.prg", "force": True,
                           "native": True, "playback_ms": 250})
    chunks = {c.kind: c.args for c in captured["job"].chunks}
    assert chunks["load"]["force"] is True
    assert chunks["record"]["frame"] is False
    assert chunks["record"]["playback_ms"] == 250


def test_a_failing_tool_call_returns_the_message_without_a_traceback(rpc, tmp_path):
    is_err, text = _tool(rpc, "ciqsim_status", {})  # warm-up: protocol still answers
    is_err, text = _tool(rpc, "ciqsim_shot", {"project": str(tmp_path / "missing")})
    assert is_err
    assert text.startswith("ERROR:") and "Traceback" not in text


def test_bump_with_an_unknown_priority_is_an_error_not_urgent(rpc, monkeypatch):
    from ciqkit.sim import client as sim_client

    def boom(*a, **k):
        raise AssertionError("must be refused before reaching the broker")
    monkeypatch.setattr(sim_client.Client, "bump", boom)
    is_err, text = _tool(rpc, "ciqsim_bump", {"job_id": "x", "priority": "bogus"})
    assert is_err and "bogus" in text


def test_a_tool_error_names_the_exception_type(rpc):
    """str(KeyError('job_id')) is just "'job_id'": without the type an agent
    cannot tell a missing argument from a broker answer."""
    is_err, text = _tool(rpc, "ciqsim_bump", {})
    assert is_err
    assert text.startswith("ERROR: KeyError") and "job_id" in text



@pytest.mark.parametrize("reply", [
    {"ok": False, "error": "no such job", "code": "no_such_job"},
    {"ok": False, "error": "no such job"},              # a broker from before codes
])
@pytest.mark.parametrize("tool", ["ciqsim_bump", "ciqsim_cancel"])
def test_a_job_the_broker_does_not_know_is_its_answer_not_a_tool_error(
        rpc, monkeypatch, tool, reply):
    from ciqkit.sim import client as sim_client
    monkeypatch.setattr(sim_client.Client, "_rpc",
                        lambda self, req, read_timeout=None: reply)
    is_err, text = _tool(rpc, tool, {"job_id": "gone"})
    assert not is_err and json.loads(text) == reply


def test_a_refused_submit_names_the_typed_error(rpc, monkeypatch, tmp_path):
    from ciqkit.sim import client as sim_client
    (tmp_path / "manifest.xml").write_bytes(b"<iq:manifest/>")
    monkeypatch.setattr(sim_client.Client, "_rpc",
                        lambda self, req, read_timeout=None: {
                            "ok": False, "error": "invalid job: nope", "code": "invalid"})
    is_err, text = _tool(rpc, "ciqsim_test", {"project": str(tmp_path),
                                              "device": "venu3", "prg": "a.prg"})
    assert is_err and text == "ERROR: BrokerError: invalid job: nope"

# --- concurrency: a long tools/call must not block the read loop ------------
# Every wait below is on an Event/Condition with a generous safety timeout; none
# is a sleep that the test's outcome depends on.

class _Out:
    """Stand-in stdout: parses each JSON-RPC line and signals its id."""

    def __init__(self):
        self.buf = ""
        self.msgs = []
        self.lock = threading.Lock()
        self.events = {}

    def answered(self, rid):
        with self.lock:
            return self.events.setdefault(rid, threading.Event())

    def write(self, s):
        with self.lock:
            self.buf += s
            while "\n" in self.buf:
                line, self.buf = self.buf.split("\n", 1)
                msg = json.loads(line)
                self.msgs.append(msg)
                self.events.setdefault(msg.get("id"), threading.Event()).set()

    def flush(self):
        pass

    def ids(self):
        with self.lock:
            return [m.get("id") for m in self.msgs]


class _FakeBroker:
    """A broker whose WAIT blocks until released or the job is cancelled --
    like the real one, which answers a WAIT as soon as the job is terminal."""

    def __init__(self):
        self.cond = threading.Condition()
        self.released = False
        self.cancelled = []
        self.submitted = threading.Event()
        self.n = 0

    def release(self):
        with self.cond:
            self.released = True
            self.cond.notify_all()

    def client_class(broker):
        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def submit(self, job):
                with broker.cond:
                    broker.n += 1
                    jid = f"job-{broker.n}"
                broker.submitted.set()
                return jid

            def wait(self, jid, timeout_s=600.0):
                with broker.cond:
                    broker.cond.wait_for(
                        lambda: broker.released or jid in broker.cancelled, 30)
                    state = "cancelled" if jid in broker.cancelled else "done"
                return {"ok": state == "done",
                        "job": {"id": jid, "state": state, "results": []}}

            def cancel(self, jid):
                with broker.cond:
                    broker.cancelled.append(jid)
                    broker.cond.notify_all()
                return {"ok": True}

        return FakeClient


@pytest.fixture
def broker(monkeypatch):
    b = _FakeBroker()
    monkeypatch.setattr(mcp_server, "Client", b.client_class())
    return b


@pytest.fixture
def out():
    # Installed as sys.stdout by _serve, not here: pytest's own capture
    # re-installs its stdout between fixture setup and the test body.
    return _Out()


@pytest.fixture
def shot_call(tmp_path):
    (tmp_path / "manifest.xml").write_text("<iq:manifest/>")
    (tmp_path / "bin").mkdir()

    def make(rid):
        return json.dumps({
            "jsonrpc": "2.0", "id": rid, "method": "tools/call",
            "params": {"name": "ciqsim_shot", "arguments": {
                "project": str(tmp_path), "name": "home",
                "prg": "bin/App.prg", "device": "fenix7"}}})
    return make


def _ping(rid):
    return json.dumps({"jsonrpc": "2.0", "id": rid, "method": "ping"})


def _cancel(rid):
    return json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                       "params": {"requestId": rid, "reason": "user aborted"}})


def _serve(lines, out):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(mcp_server.sys, "stdout", out)
        t = threading.Thread(target=mcp_server.serve, args=(lines,), daemon=True)
        t.start()
        t.join(60)
    assert not t.is_alive(), "serve() did not return at EOF"
    assert mcp_server._inflight == {}


def test_a_ping_is_answered_while_a_tool_call_is_blocked(broker, out, shot_call):
    seen = {}

    def lines():
        yield shot_call(1)
        seen["submitted"] = broker.submitted.wait(10)
        yield _ping(2)
        seen["ping"] = out.answered(2).wait(10)
        seen["call_still_blocked"] = not out.answered(1).is_set()
        broker.release()
        seen["call"] = out.answered(1).wait(10)

    _serve(lines(), out)
    assert seen == {"submitted": True, "ping": True,
                    "call_still_blocked": True, "call": True}
    assert out.ids() == [2, 1]
    assert broker.cancelled == []     # a finished call cancels nothing at EOF


def test_a_cancelled_call_cancels_its_job_and_gets_no_response(
        broker, out, shot_call):
    seen = {}

    def lines():
        yield shot_call(1)
        seen["submitted"] = broker.submitted.wait(10)
        yield _cancel(1)
        with broker.cond:
            seen["cancelled"] = broker.cond.wait_for(
                lambda: "job-1" in broker.cancelled, 10)
        yield _ping(2)
        seen["ping"] = out.answered(2).wait(10)

    _serve(lines(), out)
    assert seen == {"submitted": True, "cancelled": True, "ping": True}
    assert broker.cancelled == ["job-1"]
    assert out.ids() == [2]           # MCP: no response to a cancelled request


def test_eof_cancels_every_in_flight_job(broker, out, shot_call):
    seen = {}

    def lines():
        yield shot_call(1)
        seen["submitted"] = broker.submitted.wait(10)
        # then EOF: the client went away mid-call

    _serve(lines(), out)
    assert seen == {"submitted": True}
    assert broker.cancelled == ["job-1"]
    # Not a client cancel: the answer is still written for a half-closed
    # client, and it says the job did not finish.
    assert out.ids() == [1]
    text = out.msgs[0]["result"]["content"][0]["text"]
    assert json.loads(text)["ok"] is False


def test_a_call_finishing_after_eof_still_answers(out):
    """EOF stops new jobs; it must not swallow an answer already on its way
    (found by piping `ping; tools/call` into the real process)."""
    _serve(iter([json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                             "params": {"name": "ciqsim_bump", "arguments": {}}})]),
           out)
    assert out.ids() == [3]


def test_a_cancel_for_an_unknown_request_is_ignored(broker, out):
    _serve(iter([_cancel(42), _ping(1)]), out)
    assert out.ids() == [1] and broker.cancelled == []


def test_concurrent_responses_never_interleave(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class SlowOut:
        def __init__(self):
            self.n = 0
            self.inside = False
            self.overlap = False
            self.lines = []

        def write(self, s):
            if self.inside:
                self.overlap = True
            self.inside = True
            self.n += 1
            if self.n == 1:          # park the first writer mid-write
                entered.set()
                release.wait(10)
            self.lines.append(s)
            self.inside = False

        def flush(self):
            pass

    o = SlowOut()
    monkeypatch.setattr(mcp_server.sys, "stdout", o)
    a = threading.Thread(target=mcp_server._respond, args=(1, {}))
    a.start()
    assert entered.wait(10)
    b = threading.Thread(target=mcp_server._respond, args=(2, {}))
    b.start()
    b.join(0.5)                      # give b every chance to barge in
    assert b.is_alive(), "second response was written while the first was mid-write"
    release.set()
    a.join(10)
    b.join(10)
    assert not o.overlap
    assert [json.loads(s)["id"] for s in o.lines] == [1, 2]
