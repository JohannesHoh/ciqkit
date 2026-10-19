"""Drop credential-bearing lines from text and from a stream.

Generic on purpose: this module contains no Garmin strings, no cookie names, and
no knowledge of what a secret looks like. The caller supplies the markers, so
the same filter serves a Connect IQ dashboard log, a cloud CLI's debug output,
or anything else whose tooling prints request headers when it fails.

WHY A LINE FILTER, NOT VALUE SURGERY
------------------------------------
The leak this exists for is a whole header blob embedded in an error string:
a failing HTTP client prints the request it failed on, headers included. The
formatting is the client's business, not yours — `cookie: a=b; c=d`, a JSON
`{"cookie": "..."}`, a repr with escaped quotes, a line wrapped at 80 columns.
Excising the value inside the line means guessing the delimiter, and a wrong
guess fails *open*.

Dropping the whole line cannot fail open: if the marker is on the line, the line
goes. Over-redacting one line of a log is cheap; leaking one session cookie is
not.

WHERE TO APPLY IT
-----------------
As close to the producer as you can get. Filtering a subprocess's output in the
parent means the credential already crossed a pipe, and a child that writes to
an inherited fd 2 bypasses the filter entirely. Wrap the streams *inside* the
process that holds the credential; use this on a relayed log only as a second
line of defence.
"""
from __future__ import annotations

from collections.abc import Iterable

__all__ = ["REDACTED", "is_secret_line", "redact_text", "RedactingStream"]

REDACTED = "[redacted: credential-bearing line]"


def is_secret_line(line: str, markers: Iterable[str]) -> bool:
    """True if `line` contains any marker, compared case-insensitively."""
    low = line.lower()
    return any(m.lower() in low for m in markers)


def redact_text(text: str, markers: Iterable[str]) -> str:
    """Replace every marker-bearing line of `text` with `REDACTED`.

    Line structure is preserved — same number of lines, and a text that did not
    end in a newline does not silently grow one — so a redacted traceback still
    reads as a traceback.
    """
    if not text:
        return text
    markers = tuple(markers)
    out = []
    for line in text.splitlines(True):
        if is_secret_line(line, markers):
            out.append(REDACTED + ("\n" if line.endswith("\n") else ""))
        else:
            out.append(line)
    return "".join(out)


class RedactingStream:
    """Line-buffered proxy for a text stream that drops marker-bearing lines.

    Line buffered rather than per-write because one logical line routinely
    arrives in several `write()` calls, and a marker split across two of them is
    invisible to a per-write check.

    `flush` handles the partial tail: the last line of an error dump often has
    no trailing newline, and forwarding it unchecked is exactly the case that
    matters. Register it with `atexit` if you wrap `sys.stderr`.
    """

    def __init__(self, inner, markers: Iterable[str], marker_text: str | None = None):
        self._inner = inner
        self._markers = tuple(markers)
        # Indentation is the caller's to choose: a log that indents its lines
        # wants the marker indented too, or the redaction reads as a new section.
        self._marker_text = (marker_text if marker_text is not None
                             else REDACTED) + "\n"

    def write(self, text: str) -> int:
        self._buf = getattr(self, "_buf", "") + text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if is_secret_line(line, self._markers):
                self._inner.write(self._marker_text)
            else:
                self._inner.write(line + "\n")
        return len(text)

    def flush(self) -> None:
        buf = getattr(self, "_buf", "")
        if buf:
            if is_secret_line(buf, self._markers):
                self._inner.write(self._marker_text)
            else:
                self._inner.write(buf)
            self._buf = ""
        self._inner.flush()

    # Anything else a caller expects of a stream (isatty, encoding, fileno…)
    # belongs to the stream we wrap, not to us.
    def __getattr__(self, name):
        return getattr(self._inner, name)
