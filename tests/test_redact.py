"""Guards for ciqkit.redact — the credential line filter.

It exists because an HTTP client that fails prints the request it failed on,
headers included. The properties that matter are the ones a naive filter gets
wrong:

  * a marker split across two `write()` calls must still be caught (one logical
    line routinely arrives in several writes);
  * the partial tail with no trailing newline must be checked on flush — that is
    the last line of an error dump, the case that matters most;
  * line structure must survive, or a redacted traceback stops reading as one;
  * no markers of its own: this module must stay credential-agnostic, so the
    caller's list is the only thing that decides.
"""
import io


from ciqkit.redact import REDACTED, RedactingStream, is_secret_line, redact_text

MARKERS = ("cookie", "authorization", "fe26")
SECRET = "cookie: session=Fe26.2**deadbeef; refresh_token=cafe"


def test_a_marked_line_is_replaced_whole():
    out = redact_text(f"before\n{SECRET}\nafter\n", MARKERS)
    assert "deadbeef" not in out and "session=" not in out
    assert out == f"before\n{REDACTED}\nafter\n"


def test_matching_is_case_insensitive():
    assert is_secret_line("COOKIE: x", MARKERS)
    assert is_secret_line("Authorization: Bearer y", MARKERS)


def test_line_count_and_final_newline_are_preserved():
    text = "a\nb\nc"          # no trailing newline
    assert redact_text(text, MARKERS) == text
    assert len(redact_text(f"a\n{SECRET}\nc", MARKERS).splitlines()) == 3
    assert not redact_text("a\nb", MARKERS).endswith("\n")


def test_empty_text_is_returned_unchanged():
    assert redact_text("", MARKERS) == ""


def test_no_markers_means_nothing_is_dropped():
    # A caller that passes an empty list gets a passthrough, not a guess.
    assert redact_text(SECRET + "\n", ()) == SECRET + "\n"


def test_a_marker_split_across_writes_is_still_caught():
    inner = io.StringIO()
    s = RedactingStream(inner, MARKERS)
    s.write("coo")          # a marker cut in half by the write boundary
    s.write("kie: secret\n")
    s.flush()
    assert "secret" not in inner.getvalue()
    assert REDACTED in inner.getvalue()


def test_the_partial_tail_is_checked_on_flush():
    inner = io.StringIO()
    s = RedactingStream(inner, MARKERS)
    s.write("Traceback...\n")
    s.write(SECRET)          # no trailing newline: an error dump's last line
    assert "deadbeef" not in inner.getvalue(), "must not forward before flush"
    s.flush()
    assert "deadbeef" not in inner.getvalue()
    assert REDACTED in inner.getvalue()


def test_clean_lines_pass_through_untouched():
    inner = io.StringIO()
    s = RedactingStream(inner, MARKERS)
    s.write("  [OK] Version filled: 1.0.0\n")
    s.flush()
    assert inner.getvalue() == "  [OK] Version filled: 1.0.0\n"


def test_the_marker_text_is_the_callers_choice():
    inner = io.StringIO()
    s = RedactingStream(inner, MARKERS, marker_text="    [gone]")
    s.write(SECRET + "\n")
    s.flush()
    assert inner.getvalue() == "    [gone]\n"


def test_unknown_attributes_come_from_the_wrapped_stream():
    inner = io.StringIO()
    s = RedactingStream(inner, MARKERS)
    assert s.getvalue() == ""       # StringIO's own API, not ours


def test_this_module_defines_no_credential_markers_of_its_own():
    """No vendor strings in the CODE — prose may name examples, code may not.

    The point of the module is that the caller owns the marker list. A marker
    baked in here would silently apply to every caller, and a module that knows
    what a Garmin cookie looks like has stopped being generic.
    """
    import ast
    import pathlib

    import ciqkit.redact as mod
    tree = ast.parse(pathlib.Path(mod.__file__).read_text())
    # Docstrings are prose, not behaviour. Identify them by position — the first
    # statement of a module/class/function — rather than by comparing text:
    # ast.get_docstring() re-indents what it returns, so a text comparison
    # silently fails to match and the docstring leaks into the check.
    doc_nodes = set()
    for n in ast.walk(tree):
        body = getattr(n, "body", None)
        # `body` is a list on statements and a bare node on expressions
        # (IfExp, Lambda) — only the former can hold a docstring.
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) and \
                isinstance(body[0].value, ast.Constant) and \
                isinstance(body[0].value.value, str):
            doc_nodes.add(id(body[0].value))
    code_strings = [n.value.lower() for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and id(n) not in doc_nodes]
    for vendor in ("garmin", "fe26", "refresh_token", "sso", "x-api-key",
                   "cookie", "bearer"):
        assert not any(vendor in s for s in code_strings), (vendor, code_strings)
