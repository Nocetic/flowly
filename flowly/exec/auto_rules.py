"""Which shell commands Auto runs without asking.

Auto (``security=allowlist`` + ``ask=on-miss``) used to judge a command by its
first word only: ``echo hi && rm -r ./build`` and ``echo x | bash`` ran
unasked because ``echo`` is harmless, while ``ls -la`` and ``git status``
asked, because only sixteen stdin filters were trusted. It was both too loud
and not safe.

Now every part of a chain is judged (``|``, ``&&``, ``||``, ``;``), and a
command runs unasked only when every part reads and nothing else: a known
read-only program, with no option that writes, runs another program or
leaves the folder it runs in. Anything else asks, as before. Redirection,
command substitution, variables, background jobs and subshells always ask.

Paths given as arguments must stay inside the folder the command runs in
(symlinks resolved), and when that folder is ``/`` or the home directory
itself, no path argument passes at all.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

# Operators that join commands; each side is judged on its own.
_CHAIN = {"|", "||", "&&", ";"}
# Anything else the shell would act on asks: redirection, background jobs,
# subshells, |& and the like (punctuation tokens that are not a chain).
_SHELL_PUNCTUATION = set("();<>|&")

# Read-only programs, and the options with which they write a file, run a
# program, or change the system. A program not listed here asks.
_FILTERS = {
    "cat": set(), "echo": set(), "grep": set(), "egrep": set(), "fgrep": set(),
    "head": set(), "tail": set(), "wc": set(), "cut": set(), "tr": set(), "jq": set(),
    "sort": {"-o", "--output"},
    "uniq": set(),
    "pwd": set(), "whoami": set(), "uname": set(), "true": set(), "false": set(), "sleep": set(),
    "date": {"-s", "--set"},
    "ls": set(), "stat": set(), "du": set(), "df": set(), "file": set(),
    "which": set(), "realpath": set(), "basename": set(), "dirname": set(),
    "diff": set(), "cmp": set(), "nl": set(), "column": set(), "printenv": set(),
    "tree": {"-o"},
    "rg": {"--pre", "--pre-glob"},
    "find": {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"},
}
# Short options that write or change something, also when clustered with
# others (``sort -ro out``) or joined to their value (``sort -oout``).
_SHORT_LETTERS = {"sort": "o", "date": "s", "tree": "o"}
# Programs whose positional arguments are text, not paths.
_TEXT_ARGS = {"echo", "grep", "egrep", "fgrep", "jq", "tr", "basename", "dirname", "printenv", "which", "date", "sleep"}
# Programs that change something when given a positional argument.
_NO_POSITIONALS = {"hostname"}
# uniq with two files writes the second.
_MAX_POSITIONALS = {"uniq": 1}

# git subcommands that only read. Global options that set configuration
# (-c, --exec-path, …) can make git run programs, so they ask.
_GIT_READ = {"status", "log", "diff", "show", "blame", "rev-parse", "ls-files", "describe", "shortlog", "reflog"}
_GIT_LISTING = {"branch", "tag", "remote", "stash"}
_GIT_WRITING_OPTIONS = {"--output", "--ext-diff", "--exec"}


def _inside(cwd: Path, argument: str) -> bool:
    """Whether a path argument names something inside ``cwd``."""
    if argument.startswith("~") or ".." in Path(argument).parts:
        return False
    target = (cwd / argument) if not os.path.isabs(argument) else Path(argument)
    try:
        resolved = target.resolve(strict=False)
        root = cwd.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return resolved == root or root in resolved.parents


def _broad(cwd: Path) -> bool:
    """A folder too wide to read in silently: the filesystem or home root."""
    try:
        resolved = cwd.resolve(strict=False)
        home = Path.home().resolve(strict=False)
    except (OSError, RuntimeError):
        return True
    return resolved == Path(resolved.anchor) or resolved == home or resolved in home.parents


def _option_name(token: str) -> str:
    return token.split("=", 1)[0]


def _paths_ok(arguments: list[str], cwd: Path | None) -> bool:
    for argument in arguments:
        if argument.startswith("-"):
            continue
        looks_like_path = "/" in argument or argument.startswith((".", "~")) or (
            cwd is not None and (cwd / argument).exists()
        )
        if not looks_like_path:
            continue
        if cwd is None or _broad(cwd) or not _inside(cwd, argument):
            return False
    return True


def _git_reads(arguments: list[str], cwd: Path | None) -> bool:
    if not arguments or arguments[0].startswith("-"):
        # Global options before the subcommand (-c, -C, --git-dir…): ask.
        return False
    sub, rest = arguments[0], arguments[1:]
    if any(_option_name(arg) in _GIT_WRITING_OPTIONS for arg in rest):
        return False
    if sub in _GIT_READ:
        return _paths_ok([arg for arg in rest if "/" in arg or arg.startswith((".", "~"))], cwd)
    if sub in _GIT_LISTING:
        # Listing only: a name creates (branch, tag), a word changes (remote add, stash push).
        if sub == "remote":
            return all(arg in {"-v", "--verbose"} for arg in rest)
        if sub == "stash":
            return rest[:1] == ["list"] and all(arg.startswith("-") for arg in rest[1:])
        return all(arg.startswith("-") and arg not in {"-d", "-D", "-m", "-M", "-c", "-C", "-f", "--delete", "--force"} for arg in rest)
    return False


def _segment_reads(tokens: list[str], cwd: Path | None) -> bool:
    if not tokens:
        return False
    program, arguments = tokens[0], tokens[1:]
    if "/" in program or "=" in program:
        # ./script, /usr/bin/x, FOO=bar cmd: not a known program by name.
        return False
    if program == "git":
        return _git_reads(arguments, cwd)
    if program == "cd":
        # Into a folder inside this one only: what follows then still reads
        # inside it (paths with .. or ~ ask, links out are resolved).
        return (
            len(arguments) == 1 and not arguments[0].startswith("-")
            and cwd is not None and not _broad(cwd) and _inside(cwd, arguments[0])
        )
    if program in _NO_POSITIONALS:
        return all(arg.startswith("-") for arg in arguments)
    if program not in _FILTERS:
        return False
    if any(_option_name(arg) in _FILTERS[program] for arg in arguments):
        return False
    letters = _SHORT_LETTERS.get(program, "")
    if letters and any(
        arg.startswith("-") and not arg.startswith("--") and any(letter in arg[1:] for letter in letters)
        for arg in arguments
    ):
        return False
    positionals = [arg for arg in arguments if not arg.startswith("-")]
    if program in _MAX_POSITIONALS and len(positionals) > _MAX_POSITIONALS[program]:
        return False
    if program in _TEXT_ARGS and program not in {"grep", "egrep", "fgrep", "jq"}:
        return True
    if program in {"grep", "egrep", "fgrep", "jq"}:
        # The first positional is the pattern or filter; the rest are files.
        return _paths_ok(positionals[1:], cwd)
    return _paths_ok(positionals, cwd)


def _tokens(command: str) -> list[str] | None:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def runs_without_asking(command: str, cwd: str | None) -> bool:
    """Whether Auto may run ``command`` in ``cwd`` without asking."""
    if not command.strip() or any(mark in command for mark in ("`", "$", "\n", "\r")):
        return False
    tokens = _tokens(command)
    if not tokens:
        return False
    folder = Path(cwd) if cwd else None
    segment: list[str] = []
    segments: list[list[str]] = []
    for token in tokens:
        if token in _CHAIN:
            if not segment:
                return False
            segments.append(segment)
            segment = []
        elif set(token) <= _SHELL_PUNCTUATION:
            return False
        else:
            segment.append(token)
    if not segment:
        return False
    segments.append(segment)
    return all(_segment_reads(part, folder) for part in segments)


def is_single_command(command: str) -> bool:
    """One program with its arguments: no chain, redirection or expansion.

    The owner's own allowlist ("always allow npm") names a program; it must
    not carry whatever is chained after it (``npm test && curl … | sh``).
    """
    if any(mark in command for mark in ("`", "$", "\n", "\r")):
        return False
    tokens = _tokens(command)
    return bool(tokens) and not any(set(token) <= _SHELL_PUNCTUATION for token in tokens)
