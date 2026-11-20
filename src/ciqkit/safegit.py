"""Asking git about a directory ciqkit did not create, without running its config.

ciqkit asks git small questions about a project or a ``--prg``'s checkout
(top level, sha, dirty or not). A repository's own ``.git/config`` can turn
even those into command execution: ``core.fsmonitor`` runs on every index
refresh (``status``, ``diff``), a ``filter.<driver>.clean`` runs on any file
whose stat info changed, ``diff.external`` / textconv on ``diff``, and a
partial clone's lazy fetch runs whatever transport ``remote.*`` names.
``safe.directory`` only refuses repositories another user owns; a same-owner
tree unpacked from an archive keeps its ``.git/config``. SECURITY.md §2
promises that ``--prg`` runs nothing from the project, so every git call goes
through :func:`run`.

Command-line ``-c`` beats every config file (includes too), so each override
below is final. The system and global config are the user's own and stay.
"""
from __future__ import annotations

import os
import re
import subprocess

from . import _compat
from ._compat import hidden_console_kwargs, which_no_cwd

__all__ = ["run", "argv", "UnsafeRepo"]

# Neutralised for every call. hooksPath: no hook runs on these commands
# today, but a future one might. protocol.*: a lazy fetch (partial clone)
# would otherwise use the repo's own transport, `ext::` included.
_OVERRIDES = (
    "core.fsmonitor=false",
    f"core.hooksPath={os.devnull}",
    "protocol.allow=never",
    *(f"protocol.{p}.allow=never" for p in ("ext", "file", "ssh", "git", "http", "https")),
)

# Subcommands that compare work-tree content with the index, and so run the
# clean filter of any driver the repo configures.
_CONTENT_CMDS = frozenset({"status", "diff", "diff-files", "diff-index"})
_DIFF_CMDS = frozenset({"diff", "diff-files", "diff-index"})
_FILTER_KEYS = ("clean", "smudge", "process")


# GIT_NO_LAZY_FETCH (the switch that stops a partial clone's lazy fetch) exists
# from this git version on.
_NO_LAZY_FETCH_FROM = (2, 44)
# Config that makes a repository a partial clone (it has a promisor remote).
_PROMISOR_KEYS = re.compile(r"^(extensions\.partialclone|remote\..*\.(promisor|partialclonefilter))$", re.IGNORECASE)


class UnsafeRepo(OSError):
    """The repository configures something :func:`run` cannot neutralise."""


def _git() -> str:
    # Absolute, and never from the current directory (see which_no_cwd).
    git = which_no_cwd("git")
    if not git:
        raise FileNotFoundError("git not found on PATH")
    return git


def _env() -> dict:
    return dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0",
                GIT_NO_LAZY_FETCH="1")


def _git_is_older_than_no_lazy_fetch(git: str) -> bool:
    """True unless ``git --version`` says 2.44 or newer (unparseable: old)."""
    try:
        r = subprocess.run([git, "--version"], capture_output=True, encoding="utf-8",
                           errors="replace", env=_env(), stdin=subprocess.DEVNULL,
                           timeout=10, **hidden_console_kwargs())
        m = re.search(r"(\d+)\.(\d+)", r.stdout)
        return not m or (int(m[1]), int(m[2])) < _NO_LAZY_FETCH_FROM
    except (OSError, subprocess.SubprocessError):
        return True


def _base(git: str) -> list[str]:
    out = [git, "--no-pager"]
    for kv in _OVERRIDES:
        out += ["-c", kv]
    return out


def _filter_overrides(git: str, cwd: str) -> list[str]:
    """``-c filter.<name>.clean=`` (etc.) for every filter driver the repo defines.

    Driver names are free-form, so there is no single switch: list them (reading
    config executes nothing) and blank each one. An empty command is "no
    filter" to git; ``required=false`` keeps a blanked required filter from
    failing the command.
    """
    # -z and bytes: a driver name may hold any byte but NUL (a newline, a form
    # feed, U+2028, a non-UTF-8 byte), so neither line splitting nor a lossy
    # decode may touch it.
    r = subprocess.run(_base(git) + ["config", "-z", "--name-only", "--get-regexp",
                                    r"^(filter\.|extensions\.partialclone$|remote\..*\.(promisor|partialclonefilter)$)"],
                       cwd=cwd, capture_output=True, env=_env(), timeout=10,
                       **hidden_console_kwargs())
    names = set()
    promisor = False
    for raw in r.stdout.split(b"\0"):
        if not raw:
            continue
        key = os.fsdecode(raw)
        if _PROMISOR_KEYS.match(key):
            promisor = True
            continue
        if os.fsencode(key) != raw or not key.startswith("filter."):
            # Not passable verbatim in argv: cannot be neutralised.
            raise UnsafeRepo(f"git config key {raw!r} cannot be neutralised")
        rest = key[len("filter."):]
        if "." not in rest:
            continue  # filter.<variable>: no driver name, nothing runs
        # filter.<name>.<key>; <name> may itself contain dots, or be empty.
        names.add(rest.rpartition(".")[0])
    if promisor and _git_is_older_than_no_lazy_fetch(git):
        # A partial clone fetches a missing object on demand, through whatever
        # remote helper the repo's config names. GIT_NO_LAZY_FETCH stops that,
        # but older git ignores it, and `protocol.allow=never` is only the
        # default for a protocol the repo does not configure itself
        # (`protocol.<name>.allow=always` plus `alias.remote-<name>=!cmd`). The
        # names are free-form, so there is nothing to override: do not ask.
        raise UnsafeRepo("a partial clone cannot be queried safely with git < 2.44")
    out = []
    for name in sorted(names):
        if "=" in name or "\n" in name:
            # `-c` splits at the first "=", so this name cannot be overridden.
            raise UnsafeRepo(f"git filter driver {name!r} cannot be neutralised")
        for key in _FILTER_KEYS:
            out += ["-c", f"filter.{name}.{key}="]
        out += ["-c", f"filter.{name}.required=false"]
    return out


def argv(cwd: str, *args: str) -> list[str]:
    """The hardened argv for ``git <args>`` run in ``cwd``."""
    git = _git()
    cmd = _base(git)
    if not args:
        return cmd
    sub, rest = args[0], list(args[1:])
    if sub in _CONTENT_CMDS:
        cmd += _filter_overrides(git, cwd)
    if sub in _DIFF_CMDS:
        # External diff and textconv drivers are config too.
        rest = ["--no-ext-diff", "--no-textconv", *rest]
    if sub == "status":
        # A submodule is a repository of its own, with its own filters.
        rest = ["--ignore-submodules=all", *rest]
    return cmd + [sub, *rest]


def run(cwd: str, *args: str, timeout: float = 20) -> subprocess.CompletedProcess:
    """``git <args>`` in ``cwd`` with the repository's executable config disabled.

    Output is captured as text. Raises OSError (FileNotFoundError without git,
    :class:`UnsafeRepo`) and subprocess.TimeoutExpired like subprocess.run.
    run_killing_tree, not subprocess.run: Git for Windows' cmd\\git.exe hands
    its pipes to a child, and subprocess.run waits on them untimed after
    killing only the launcher.
    """
    return _compat.run_killing_tree(argv(cwd, *args), cwd=cwd, capture_output=True,
                                    encoding="utf-8", errors="replace", env=_env(),
                                    stdin=subprocess.DEVNULL, timeout=timeout,
                                    **hidden_console_kwargs())
