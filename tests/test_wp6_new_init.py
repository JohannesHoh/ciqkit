"""`ciqkit new` / `ciqkit init`: templates, scaffolding, merging. No SDK needed."""
import json
import os
import re
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from ciqkit import build, init_cmd, init_cli, new_cli, scaffold

REPO = Path(__file__).resolve().parent.parent
NS = "{http://www.garmin.com/xml/connectiq}"


@pytest.fixture
def devdir(tmp_path):
    d = tmp_path / "devices"
    for n in ("venu3", "fr255"):
        (d / n).mkdir(parents=True)
    return str(d)


@pytest.fixture(autouse=True)
def fake_key(tmp_path, monkeypatch):
    monkeypatch.setenv("CIQKIT_DEV_KEY", str(tmp_path / "keys" / "k.der"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    calls = []

    def fake_gen(path=None, bits=4096):
        path = path or build.dev_key_path()
        calls.append(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"k")
        return path

    monkeypatch.setattr(build, "generate_dev_key", fake_gen)
    return calls


def _new(tmp_path, devdir, name="my-app", **kw):
    return scaffold.create(name, parent_dir=str(tmp_path / "out"), devices_dir=devdir, **kw)


@pytest.mark.parametrize("template,type_", [
    ("blank", "app"), ("blank", "watchface"), ("blank", "datafield"), ("tictactoe", "app")])
def test_new_creates_valid_project(tmp_path, devdir, template, type_):
    res = _new(tmp_path, devdir, template=template, type_=type_)
    proj = Path(res.project)
    for f in ("manifest.xml", "monkey.jungle", "ciqkit.toml", ".mcp.json", "CLAUDE.md",
              ".gitignore", "resources/strings/strings.xml"):
        assert (proj / f).is_file(), f
    assert list((proj / "source").rglob("*.mc"))
    root = ET.parse(proj / "manifest.xml").getroot()
    app = root.find(f"{NS}application")
    assert uuid.UUID(app.get("id")).version == 4 and app.get("id") == res.uuid
    assert app.get("type") == scaffold.TYPES[type_]
    # tictactoe's README and CLAUDE.md describe its three devices, so it
    # lists all three by default; blank projects start with one.
    expected = list(scaffold.TICTACTOE_DEVICES) if template == "tictactoe" else ["venu3"]
    assert [p.get("id") for p in root.iter(f"{NS}product")] == expected
    assert 'device = "venu3"' in (proj / "ciqkit.toml").read_text()
    for p in proj.rglob("*"):
        if p.is_file() and p.suffix not in (".png",):
            assert "{{" not in p.read_text(), p
        assert "{{" not in p.name
    assert not (proj / "scripts" / "build.sh").exists()
    assert not (proj / "dot_gitignore").exists()


def test_blank_types_use_matching_base_classes(tmp_path, devdir):
    want = {"app": "WatchUi.View", "watchface": "WatchUi.WatchFace",
            "datafield": "WatchUi.DataField"}
    for t, base in want.items():
        res = _new(tmp_path, devdir, name=f"p-{t}", type_=t)
        view = next(Path(res.project).glob("source/*View.mc"))
        assert f"extends {base}" in view.read_text()
        assert (Path(res.project) / "source/tests").is_dir()
        assert "(:test)" in next(Path(res.project).glob("source/tests/*.mc")).read_text()


def test_fresh_uuid_each_time(tmp_path, devdir):
    a = _new(tmp_path, devdir, name="a")
    b = _new(tmp_path, devdir, name="b")
    assert a.uuid != b.uuid


def test_class_identifier_is_safe(tmp_path, devdir):
    assert scaffold.class_id("my-cool_app") == "MyCoolApp"
    assert scaffold.class_id("9lives") == "App9lives"
    res = _new(tmp_path, devdir, name="9lives")
    assert (Path(res.project) / "source" / "App9livesApp.mc").is_file()


def test_invalid_name_and_combos(tmp_path, devdir):
    with pytest.raises(scaffold.ScaffoldError):
        _new(tmp_path, devdir, name="bad name")
    with pytest.raises(scaffold.ScaffoldError):
        _new(tmp_path, devdir, template="tictactoe", type_="watchface")


def test_refuses_non_empty_dir_but_takes_empty(tmp_path, devdir):
    out = tmp_path / "out"
    (out / "x").mkdir(parents=True)
    (out / "x" / "keep.txt").write_text("hi")
    with pytest.raises(scaffold.ScaffoldError, match="not an empty"):
        scaffold.create("x", parent_dir=str(out), devices_dir=devdir)
    assert (out / "x" / "keep.txt").read_text() == "hi"
    (out / "y").mkdir()
    scaffold.create("y", parent_dir=str(out), devices_dir=devdir)
    assert (out / "y" / "manifest.xml").is_file()


def test_devices_validated_with_warnings(tmp_path, devdir):
    res = _new(tmp_path, devdir, devices=["fr255", "nosuch"])
    assert res.devices == ["fr255", "nosuch"]
    assert any("nosuch" in w for w in res.warnings)
    assert 'device = "fr255"' in (Path(res.project) / "ciqkit.toml").read_text()
    res = scaffold.create("z", parent_dir=str(tmp_path / "o2"),
                          devices_dir=str(tmp_path / "missing"), devices=["venu3"])
    assert any("could not check" in w for w in res.warnings)


def test_default_device_prefers_venu3_else_first_installed(tmp_path):
    d = tmp_path / "dd"
    (d / "fr955").mkdir(parents=True)
    devs, warns = scaffold.choose_devices(None, str(d))
    assert devs == ["fr955"] and warns
    (d / "venu3").mkdir()
    assert scaffold.choose_devices(None, str(d))[0] == ["venu3"]
    devs, warns = scaffold.choose_devices(None, str(tmp_path / "none"))
    assert devs == ["venu3"] and warns


def test_keygen_created_existing_and_skipped(tmp_path, devdir, fake_key, monkeypatch):
    res = _new(tmp_path, devdir, name="k1")
    assert res.key_status == "created" and len(fake_key) == 1
    res = _new(tmp_path, devdir, name="k2")
    assert res.key_status == "existing" and len(fake_key) == 1

    os.remove(res.key)

    def boom(path=None, bits=4096):
        raise build.BuildError("`openssl` not found on PATH")

    monkeypatch.setattr(build, "generate_dev_key", boom)
    res = _new(tmp_path, devdir, name="k3")
    assert res.key_status == "skipped" and any("openssl" in w for w in res.warnings)


def test_launcher_icon_is_a_png(tmp_path, devdir):
    res = _new(tmp_path, devdir)
    data = (Path(res.project) / "resources/drawables/launcher_icon.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and b"IHDR" in data


def test_new_cli_prints_json(tmp_path, devdir, capsys, monkeypatch):
    monkeypatch.setattr(build, "sdk_devices_dir", lambda: devdir)
    rc = new_cli.main(["cli-app", "--dir", str(tmp_path / "c"), "--devices", "venu3,fr255"])
    cap = capsys.readouterr()
    assert rc == 0
    out = json.loads(cap.out)
    assert out["ok"] and out["devices"] == ["venu3", "fr255"]
    assert "next steps" in cap.err
    assert new_cli.main(["cli-app", "--dir", str(tmp_path / "c")]) == 1


def _example_params(devs):
    return scaffold.params_for("tictactoe", type_="app", devices=devs,
                               app_uuid="8176b20a-7099-4b48-b3d0-33d95eaa0a3e",
                               dir_name="tictactoe")


def test_tictactoe_template_matches_example():
    """The shipped template is the example, modulo placeholders. Re-copy on drift."""
    ex = REPO / "examples" / "tictactoe"
    params = _example_params(["venu3", "fr255", "venusq2"])
    files = scaffold.template_files("tictactoe", "app", params)
    skip = {".mcp.json"}  # intentionally differs: npx server vs repo-relative script
    example_files = {
        p.relative_to(ex).as_posix(): p for p in ex.rglob("*")
        if p.is_file() and "bin" not in p.relative_to(ex).parts
        and p.name != ".DS_Store" and "__pycache__" not in p.parts}
    assert set(files) - skip == set(example_files) - skip
    for rel, data in files.items():
        if rel in skip:
            continue
        assert data == example_files[rel].read_bytes(), rel


def test_template_license_note():
    lic = (REPO / "src/ciqkit/templates/LICENSE").read_text()
    assert lic.startswith("MIT No Attribution")


# ── init ───────────────────────────────────────────────────────────────────

@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "existing"
    p.mkdir()
    (p / "manifest.xml").write_text(
        '<iq:manifest version="3" xmlns:iq="http://www.garmin.com/xml/connectiq">'
        '<iq:application id="x" type="watch-app"><iq:products>'
        '<iq:product id="fr255"/></iq:products></iq:application></iq:manifest>')
    (p / "monkey.jungle").write_text("project.manifest = manifest.xml\n")
    return p


def snapshot(p):
    return {str(f.relative_to(p)): f.read_bytes() for f in p.rglob("*") if f.is_file()}


def test_init_dry_run_changes_nothing(proj, capsys):
    before = snapshot(proj)
    assert init_cli.main([str(proj), "--ci"]) == 0
    out = capsys.readouterr().out
    assert snapshot(proj) == before
    assert "dry run" in out and "+++ b/.mcp.json" in out and "ciqkit-checks.yml" in out


def test_init_requires_manifest(tmp_path, capsys):
    assert init_cli.main([str(tmp_path)]) == 1


def test_init_write_merges_mcp_preserving_servers(proj):
    (proj / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"mine": {"command": "x"}}, "other": 1}))
    init_cmd.write(init_cmd.plan(str(proj)))
    data = json.loads((proj / ".mcp.json").read_text())
    assert data["mcpServers"]["mine"] == {"command": "x"} and data["other"] == 1
    assert data["mcpServers"]["ciqsim"]["command"] == "ciqkit-mcp"
    assert data["mcpServers"]["garmin-sdk"]["command"] == "npx"


def test_init_mcp_existing_sdk_server_not_duplicated(proj):
    (proj / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"sdk-mcp": {"command": "y"}, "ciqsim": {"command": "z"}}}))
    plan = init_cmd.plan(str(proj))
    assert [c for c in plan.changes if c.path == ".mcp.json"][0].action == "skip"


def test_init_invalid_mcp_json_left_alone(proj):
    (proj / ".mcp.json").write_text("{nope")
    init_cmd.write(init_cmd.plan(str(proj)))
    assert (proj / ".mcp.json").read_text() == "{nope"


def test_init_is_idempotent(proj):
    (proj / "CLAUDE.md").write_text("# Mine\n\nkeep this\n")
    (proj / ".gitignore").write_text("bin\n*.prg\nnotes.txt")
    init_cmd.write(init_cmd.plan(str(proj), ci=True))
    first = snapshot(proj)
    plan = init_cmd.plan(str(proj), ci=True)
    assert plan.pending == []
    init_cmd.write(plan)
    assert snapshot(proj) == first
    claude = (proj / "CLAUDE.md").read_text()
    assert claude.startswith("# Mine\n\nkeep this\n") and claude.count(init_cmd.BLOCK_BEGIN) == 1
    gi = (proj / ".gitignore").read_text().splitlines()
    assert gi.count("*.prg") == 1 and gi.count("bin") == 1 and "bin/" not in gi
    assert "*.der" in gi and "notes.txt" in gi


def test_claude_block_replaced_without_touching_outside(proj):
    stale = f"top\n\n{init_cmd.BLOCK_BEGIN}\nOLD\n{init_cmd.BLOCK_END}\n\nbottom\n"
    (proj / "CLAUDE.md").write_text(stale)
    init_cmd.write(init_cmd.plan(str(proj)))
    new = (proj / "CLAUDE.md").read_text()
    assert new.startswith("top\n\n") and new.endswith("\n\nbottom\n") and "OLD" not in new
    assert init_cmd.apply_block(new) == new


def test_existing_toml_and_workflow_untouched(proj):
    (proj / "ciqkit.toml").write_text("# mine\n")
    wf = proj / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ciqkit-checks.yml").write_text("mine: 1\n")
    plan = init_cmd.plan(str(proj), ci=True)
    init_cmd.write(plan)
    assert (proj / "ciqkit.toml").read_text() == "# mine\n"
    assert (wf / "ciqkit-checks.yml").read_text() == "mine: 1\n"
    acts = {c.path: c.action for c in plan.changes}
    assert acts["ciqkit.toml"] == "skip" and acts[init_cmd.WORKFLOW_PATH] == "skip"


def test_init_ci_workflow_created_and_toml_proposed(proj):
    init_cmd.write(init_cmd.plan(str(proj), ci=True))
    wf = (proj / init_cmd.WORKFLOW_PATH).read_text()
    assert "ciqkit[checks]" in wf and "ciqkit layout" in wf and "ciqkit shot-score" in wf
    toml = (proj / "ciqkit.toml").read_text()
    assert 'device = "fr255"' in toml and "[build]" not in toml
    # without --ci no workflow
    other = proj.parent / "o"
    other.mkdir()
    (other / "manifest.xml").write_text((proj / "manifest.xml").read_text())
    init_cmd.write(init_cmd.plan(str(other)))
    assert not (other / ".github").exists()


def test_init_detects_custom_build(proj):
    (proj / "scripts").mkdir()
    (proj / "scripts" / "build.sh").write_text("#!/bin/sh\n")
    init_cmd.write(init_cmd.plan(str(proj)))
    toml = (proj / "ciqkit.toml").read_text()
    assert 'cmd = "./scripts/build.sh -d {device}"' in toml
    assert re.search(r"^\[build\]", toml, re.M)
    from ciqkit.config import load_config
    assert load_config(str(proj)).build_cmd == "./scripts/build.sh -d {device}"


def test_init_guessed_build_is_commented_out(proj):
    (proj / "Makefile").write_text("all:\n")
    init_cmd.write(init_cmd.plan(str(proj)))
    from ciqkit.config import load_config
    assert load_config(str(proj)).build_cmd is None
    assert "# cmd = " in (proj / "ciqkit.toml").read_text()


# ── WP9 review: injection and symlinks ─────────────────────────────────────

@pytest.mark.parametrize("bad", ['venu3"/><x', "fr 255", "a'b", "../x"])
def test_new_refuses_device_ids_that_would_break_the_manifest(tmp_path, devdir, bad):
    """Device ids land verbatim in a manifest attribute and a TOML string."""
    with pytest.raises(scaffold.ScaffoldError, match="invalid device id"):
        _new(tmp_path, devdir, devices=["venu3", bad])
    assert not (tmp_path / "out" / "my-app").exists()


@pytest.mark.parametrize("name", ["CLAUDE.md", ".gitignore", ".mcp.json", "ciqkit.toml"])
def test_init_never_writes_through_a_symlink(proj, tmp_path, name):
    outside = tmp_path / "outside.txt"
    outside.write_text("precious\n")
    (proj / name).symlink_to(outside)
    plan = init_cmd.plan(str(proj))
    ch = [c for c in plan.changes if c.path == name][0]
    assert ch.action == "skip" and "symlink" in ch.note
    init_cmd.write(plan)
    assert outside.read_text() == "precious\n"


def test_init_does_not_create_through_a_dangling_symlink(proj, tmp_path):
    target = tmp_path / "elsewhere" / "ciqkit.toml"
    (proj / "ciqkit.toml").symlink_to(target)
    init_cmd.write(init_cmd.plan(str(proj)))
    assert not target.exists()


def test_init_ci_skips_a_symlinked_github_dir(proj, tmp_path):
    other = tmp_path / "other-repo-github"
    other.mkdir()
    (proj / ".github").symlink_to(other)
    plan = init_cmd.plan(str(proj), ci=True)
    wf = [c for c in plan.changes if c.path == init_cmd.WORKFLOW_PATH][0]
    assert wf.action == "skip"
    init_cmd.write(plan)
    assert list(other.iterdir()) == []


def test_init_write_rechecks_symlinks(proj, tmp_path):
    """A plan computed before the tree changed must not write through a link."""
    plan = init_cmd.plan(str(proj))
    outside = tmp_path / "late.txt"
    outside.write_text("x")
    (proj / "CLAUDE.md").symlink_to(outside)
    with pytest.raises(init_cmd.InitError, match="symlink"):
        init_cmd.write(plan)
    assert outside.read_text() == "x"


def test_init_flags_unmergeable_files_and_exits_nonzero(proj, capsys):
    (proj / ".mcp.json").write_text("{nope")
    assert init_cli.main([str(proj), "--write"]) == 1
    out = capsys.readouterr().out
    assert "ATTENTION" in out and "manual attention" in out
    assert (proj / ".mcp.json").read_text() == "{nope"


def test_init_treats_an_empty_mcp_json_as_empty(proj):
    (proj / ".mcp.json").write_text("\n")
    init_cmd.write(init_cmd.plan(str(proj)))
    assert "ciqsim" in json.loads((proj / ".mcp.json").read_text())["mcpServers"]


def test_init_recognises_the_sdk_server_by_its_command(proj):
    (proj / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "docs": {"command": "npx", "args": ["-y", "ciqkit-sdk-mcp"]}}}))
    init_cmd.write(init_cmd.plan(str(proj)))
    servers = json.loads((proj / ".mcp.json").read_text())["mcpServers"]
    assert set(servers) == {"docs", "ciqsim"}


def test_claude_md_unmatched_begin_marker_is_left_alone(proj):
    text = f"# Mine\n{init_cmd.BLOCK_BEGIN}\nstuff I wrote\n"
    (proj / "CLAUDE.md").write_text(text)
    plan = init_cmd.plan(str(proj))
    ch = [c for c in plan.changes if c.path == "CLAUDE.md"][0]
    assert ch.action == "skip" and ch.attention
    init_cmd.write(plan)
    assert (proj / "CLAUDE.md").read_text() == text


def test_claude_md_stray_end_marker_converges(proj):
    (proj / "CLAUDE.md").write_text(f"# Mine\n{init_cmd.BLOCK_END}\nmore\n")
    init_cmd.write(init_cmd.plan(str(proj)))
    first = (proj / "CLAUDE.md").read_text()
    assert init_cmd.plan(str(proj)).pending == []
    assert first.count(init_cmd.BLOCK_BEGIN) == 1 and "more\n" in first


def test_claude_md_crlf_is_kept(proj):
    (proj / "CLAUDE.md").write_bytes(b"# Mine\r\nkeep\r\n")
    init_cmd.write(init_cmd.plan(str(proj)))
    data = (proj / "CLAUDE.md").read_bytes()
    assert data.startswith(b"# Mine\r\nkeep\r\n")
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert init_cmd.plan(str(proj)).pending == []
