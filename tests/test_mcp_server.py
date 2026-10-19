"""The MCP surface, exercised without a simulator.

This is the interface a coding agent drives, so its framing, schemas and
argument validation matter as much as the capture itself. Everything here runs
in-process against the JSON-RPC handler; the capture path is covered by
`test_scheduler.py` and by driving a real simulator by hand.
"""
import json

import pytest

from ciqkit.sim import mcp_server


@pytest.fixture
def rpc(capsys):
    """Send one JSON-RPC message and return the parsed reply (None if a
    notification produced no output)."""
    def send(msg):
        capsys.readouterr()
        mcp_server._handle(msg)
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
