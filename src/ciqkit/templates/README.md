# Project templates

Package data for `ciqkit new`. Everything under this directory is released
under MIT-0 (see `LICENSE`), the same terms as `examples/`: what `ciqkit new`
generates is yours, with no attribution required.

* `blank/` -- a minimal app, watch face or data field. `common/` is copied for
  every type, then `app/`, `watchface/` or `datafield/` is laid on top.
* `tictactoe/` -- a copy of `examples/tictactoe/` (kept identical by a test).

Files may contain `{{PLACEHOLDERS}}` (also in file names). A path component
starting with `dot_` is written with a leading `.` instead, so packaging tools
cannot mistake a template's `.gitignore` for a rule about this repository.
