"""Which shell commands Auto runs without asking.

Auto (``security=allowlist`` + ``ask=on-miss``) used to judge a command by its
first word only: ``echo hi && rm -r ./build`` and ``echo x | bash`` ran
unasked because ``echo`` is harmless, while ``ls -la`` and ``git status``
asked, because only sixteen stdin filters were trusted. It was both too loud
and not safe.

Every part of a chain is judged (``|``, ``&&``, ``||``, ``;``), and a command
runs unasked when every part either reads and nothing else (a known read-only
program, with no option that writes, runs another program or follows links)
or runs a program the owner chose to always allow. Command substitution,
variables other than ``$HOME``, background jobs, subshells and redirection
other than to ``/dev/null`` always ask.

A read reaches what the agent's read tools reach (``read_file``): the home
folder, the workspace and the folder the command starts in, never a protected
path. Reads used to have to stay inside the starting folder, so
``cat "$HOME/notes/x.md"`` asked every time although ``read_file`` read the
same file unasked. A search that walks down (``grep -r``, ``rg``) asks when
the folder it walks holds a protected path.

"Always allow" remembers the program of every part that does not read. It
used to remember the first program only, and the allowlist was consulted for
a single command only, so on a chain or a command with ``$HOME`` the button
did nothing and the same prompt came back.
"""

from __future__ import annotations

import glob
import os
import re
import shlex
from pathlib import Path
from typing import Callable

# Operators that join commands; each side is judged on its own.
_CHAIN = {"|", "||", "&&", ";"}
# Anything else the shell would act on asks: redirection, background jobs,
# subshells, |& and the like (punctuation tokens that are not a chain).
_SHELL_PUNCTUATION = set("();<>|&")
# Redirections that write nowhere: output to /dev/null (``2>/dev/null``).
_TO_NULL = {">", ">>", "&>", "&>>"}
_GLOB = re.compile(r"[*?\[]")

# Read-only programs, and the options with which they write a file, run a
# program, or follow links out of what they were given. A program not listed
# here does not read.
_FILTERS = {
    "cat": set(), "echo": set(), "printf": set(),
    "grep": {"-R", "--dereference-recursive"}, "egrep": {"-R", "--dereference-recursive"},
    "fgrep": {"-R", "--dereference-recursive"},
    "head": set(), "tail": set(), "wc": set(), "cut": set(), "tr": set(), "jq": set(),
    "sort": {"-o", "--output"},
    "uniq": set(),
    "pwd": set(), "whoami": set(), "id": set(), "uname": set(), "sw_vers": set(), "uptime": set(),
    "true": set(), "false": set(), "sleep": set(),
    "date": {"-s", "--set"},
    "ls": set(), "stat": set(), "du": set(), "df": set(), "file": set(), "readlink": set(),
    "which": set(), "realpath": set(), "basename": set(), "dirname": set(),
    "diff": {"-r", "--recursive"}, "cmp": set(), "nl": set(), "column": set(), "printenv": set(),
    "tree": {"-o"},
    "rg": {"--pre", "--pre-glob", "-L", "--follow"},
    "find": {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls", "-L"},
}
# Short options that write or follow links, also when clustered with others
# (``sort -ro out``, ``grep -Rn``) or joined to their value (``sort -oout``).
_SHORT_LETTERS = {"sort": "o", "date": "s", "tree": "o", "grep": "R", "egrep": "R", "fgrep": "R", "rg": "L", "diff": "r"}
# Programs whose positional arguments are text, not paths.
_TEXT_ARGS = {"echo", "printf", "tr", "basename", "dirname", "printenv", "which", "date", "sleep", "id"}
# Programs that change something when given a positional argument.
_NO_POSITIONALS = {"hostname"}
# uniq with two files writes the second.
_MAX_POSITIONALS = {"uniq": 1}
# Programs that read the folder they run in when given no path.
_READS_FOLDER = {"ls", "du", "tree", "find", "rg"}
# sed reads when it only prints lines by number: ``sed -n '1,40p' file``.
# Any other script can write (``w``), read another file (``r``) or run (``e``).
_SED_PRINT = re.compile(r"^(\d+|\$)(,(\d+|\$))?p(;(\d+|\$)(,(\d+|\$))?p)*$")

# git subcommands that only read. Global options that set configuration
# (-c, --exec-path, …) can make git run programs, so they ask.
_GIT_READ = {"status", "log", "diff", "show", "blame", "rev-parse", "ls-files", "describe", "shortlog", "reflog"}
_GIT_LISTING = {"branch", "tag", "remote", "stash"}
_GIT_WRITING_OPTIONS = {"--output", "--ext-diff", "--exec"}

# Programs that run whatever they are given: a shell, an interpreter, a
# program that starts another. Always allowing one allows everything, so it
# is never remembered, and an old allowlist entry for one is not used.
_RUNS_ANYTHING = {
    "sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh",
    "sudo", "doas", "su", "env", "xargs", "nohup", "timeout", "time", "nice",
    "exec", "eval", "command", "builtin", "source", ".",
    "osascript", "awk", "gawk", "mawk", "nawk", "find",
    "npx", "bunx", "uvx", "pipx", "deno", "bun",
}
_INTERPRETER = re.compile(r"^(python|pypy|node|ruby|perl|php|lua)[0-9.]*$")

Remembered = Callable[[str], bool]


# ── The command as the shell will run it ─────────────────────────────────────


def _expand_home(command: str) -> str | None:
    """``command`` with ``$HOME`` written out, or ``None`` if the shell would expand anything else.

    ``$`` and backticks inside single quotes are plain text; anywhere else
    only ``$HOME`` / ``${HOME}`` is known.
    """
    home = str(Path.home())
    out: list[str] = []
    single = double = False
    i = 0
    while i < len(command):
        char = command[i]
        if char == "\\" and not single:
            out.append(command[i:i + 2])
            i += 2
            continue
        if char == "'" and not double:
            single = not single
        elif char == '"' and not single:
            double = not double
        elif char == "`" and not single:
            return None
        elif char == "$" and not single:
            name = re.match(r"\$(?:\{HOME\}|HOME(?![A-Za-z0-9_]))", command[i:])
            # A home the shell would split or glob cannot be written in.
            if name is None or not re.fullmatch(r"[A-Za-z0-9._/-]+", home):
                return None
            out.append(home)
            i += len(name.group(0))
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _tokens(command: str) -> list[str] | None:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _segments(command: str) -> list[tuple[list[str], list[str]]] | None:
    """The commands of a chain, each with the files it reads through ``<``.

    ``None`` when the shell would do anything this module does not judge.
    """
    if not command.strip() or "\n" in command or "\r" in command:
        return None
    expanded = _expand_home(command)
    tokens = _tokens(expanded) if expanded is not None else None
    if not tokens:
        return None
    segments: list[tuple[list[str], list[str]]] = []
    words: list[str] = []
    inputs: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        following = tokens[i + 1] if i + 1 < len(tokens) else None
        if token in _CHAIN:
            if not words:
                return None
            segments.append((words, inputs))
            words, inputs = [], []
        elif token in _TO_NULL and following == "/dev/null":
            i += 1
        elif token == ">&" and following in {"1", "2"}:
            i += 1
        elif token == "<" and following is not None and not set(following) <= _SHELL_PUNCTUATION:
            inputs.append(following)
            i += 1
        elif set(token) <= _SHELL_PUNCTUATION:
            return None
        else:
            words.append(token)
        i += 1
    if not words:
        return None
    segments.append((words, inputs))
    return segments


# ── What a command may reach ─────────────────────────────────────────────────


def _resolve(path: Path) -> Path | None:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _granted(cwd: Path) -> Path | None:
    """The folder a command starts in, as a place it may read.

    Not when it is the filesystem or home root, or holds every agent's data.
    """
    resolved = _resolve(cwd)
    home = _resolve(Path.home())
    if resolved is None or home is None:
        return None
    if resolved == Path(resolved.anchor) or resolved == home or resolved in home.parents:
        return None
    profiles = _resolve(home / ".flowly" / "profiles")
    if profiles is not None and (profiles == resolved or resolved in profiles.parents):
        return None
    return resolved


def _readable(path: Path, granted: Path | None) -> bool:
    from flowly.agent.tools.filesystem import _is_read_allowed

    resolved = _resolve(path)
    return resolved is not None and _is_read_allowed(resolved, granted)


def _floor() -> list[Path]:
    """Paths no command may reach: the protected roots and Flowly's own auth files."""
    from flowly.agent.tools.filesystem import _get_denied_paths
    from flowly.protected_paths import _hardcoded_protected_roots

    return [root for root in map(_resolve, (*_hardcoded_protected_roots(), *_get_denied_paths())) if root is not None]


def _protected(path: Path) -> bool:
    resolved = _resolve(path)
    return resolved is None or any(root == resolved or root in resolved.parents for root in _floor())


def _holds_protected(folder: Path) -> bool:
    """Whether a protected path lies inside ``folder``: a search walking it would read it."""
    resolved = _resolve(folder)
    return resolved is None or any(root == resolved or resolved in root.parents for root in _floor())


def _candidates(argument: str, folder: Path) -> list[Path] | None:
    """Every path ``argument`` may name from ``folder``, or ``None`` if that cannot be known."""
    if "{" in argument or "}" in argument:
        return None
    if os.path.isabs(argument):
        paths = [Path(argument)]
    else:
        paths = [folder / argument]
        if argument.startswith("~"):
            # Quoted, the shell keeps ``~`` as a name; unquoted it is home.
            paths.append(Path(argument).expanduser())
    if _GLOB.search(argument):
        for pattern in list(paths):
            paths.extend(Path(match) for match in glob.glob(str(pattern)))
    return paths


def _looks_like_path(argument: str, folders: list[Path]) -> bool:
    # A brace the shell expands (``{..,x}``) may name anything.
    expands = "{" in argument and ("," in argument or ".." in argument)
    return expands or "/" in argument or argument.startswith((".", "~")) or any((f / argument).exists() for f in folders)


def _path_arguments(arguments: list[str], folders: list[Path]) -> list[str]:
    """The arguments that name paths, an option's value included (``--file=x``)."""
    paths: list[str] = []
    for argument in arguments:
        if argument.startswith("-"):
            if "=" not in argument:
                continue
            argument = argument.split("=", 1)[1]
        if argument and _looks_like_path(argument, folders):
            paths.append(argument)
    return paths


def _reads_ok(paths: list[str], folders: list[Path], granted: Path | None, walks: bool) -> bool:
    """Whether every path is readable from every folder the command may be in."""
    for argument in paths:
        for folder in folders:
            candidates = _candidates(argument, folder)
            if candidates is None:
                return False
            for path in candidates:
                if not _readable(path, granted) or (walks and _holds_protected(path)):
                    return False
    return True


def _reaches_protected(paths: list[str], folders: list[Path], walks: bool) -> bool:
    for argument in paths:
        for folder in folders:
            candidates = _candidates(argument, folder)
            if candidates is None:
                return True
            if any(_protected(path) or (walks and _holds_protected(path)) for path in candidates):
                return True
    return False


def _option_name(token: str) -> str:
    return token.split("=", 1)[0]


def _walks(program: str, arguments: list[str]) -> bool:
    """Whether the program reads every file below a folder it is given."""
    if program == "rg":
        return True
    if program in {"grep", "egrep", "fgrep"}:
        return "recurse" in arguments or any(
            arg in {"--recursive", "--directories=recurse"}
            or (arg.startswith("-") and not arg.startswith("--") and "r" in arg[1:])
            for arg in arguments
        )
    return False


# ── One command ──────────────────────────────────────────────────────────────


def _git_reads(arguments: list[str], folders: list[Path], granted: Path | None) -> bool:
    if not arguments or arguments[0].startswith("-"):
        # Global options before the subcommand (-c, -C, --git-dir…): ask.
        return False
    if not _reads_ok(["."], folders, granted, False):
        return False
    sub, rest = arguments[0], arguments[1:]
    if any(_option_name(arg) in _GIT_WRITING_OPTIONS for arg in rest):
        return False
    if sub in _GIT_READ:
        return _reads_ok([arg for arg in rest if "/" in arg or arg.startswith((".", "~"))], folders, granted, False)
    if sub in _GIT_LISTING:
        # Listing only: a name creates (branch, tag), a word changes (remote add, stash push).
        if sub == "remote":
            return all(arg in {"-v", "--verbose"} for arg in rest)
        if sub == "stash":
            return rest[:1] == ["list"] and all(arg.startswith("-") for arg in rest[1:])
        return all(arg.startswith("-") and arg not in {"-d", "-D", "-m", "-M", "-c", "-C", "-f", "--delete", "--force"} for arg in rest)
    return False


def _sed_prints(arguments: list[str]) -> tuple[bool, list[str]]:
    """Whether a sed call only prints lines by number, and the files it reads."""
    scripts: list[str] = []
    files: list[str] = []
    quiet = False
    i = 0
    while i < len(arguments):
        arg = arguments[i]
        if arg in {"-n", "--quiet", "--silent"}:
            quiet = True
        elif arg in {"-E", "-r"}:
            pass
        elif arg == "-e" and i + 1 < len(arguments):
            scripts.append(arguments[i + 1])
            i += 1
        elif arg.startswith("-"):
            return False, []
        elif not scripts:
            scripts.append(arg)
        else:
            files.append(arg)
        i += 1
    return quiet and bool(scripts) and all(_SED_PRINT.match(s.replace(" ", "")) for s in scripts), files


def _segment_reads(tokens: list[str], folders: list[Path], granted: Path | None) -> bool:
    program, arguments = tokens[0], tokens[1:]
    if "/" in program or "=" in program:
        # ./script, /usr/bin/x, FOO=bar cmd: not a known program by name.
        return False
    if program == "git":
        return _git_reads(arguments, folders, granted)
    if program in _NO_POSITIONALS:
        return all(arg.startswith("-") for arg in arguments)
    if program == "sed":
        prints, files = _sed_prints(arguments)
        return prints and _reads_ok(files, folders, granted, False)
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
    if program in _TEXT_ARGS:
        return True
    positionals = [arg for arg in arguments if not arg.startswith("-")]
    if program in _MAX_POSITIONALS and len(positionals) > _MAX_POSITIONALS[program]:
        return False
    walks = _walks(program, arguments)
    # grep's and jq's first word is a pattern unless -e/-f gave one; it is
    # judged as a path anyway when it looks like one, which only asks more.
    paths = _path_arguments(arguments, folders)
    if not any(_looks_like_path(arg, folders) for arg in positionals) and (program in _READS_FOLDER or walks):
        paths.append(".")
    return _reads_ok(paths, folders, granted, walks)


def _cd_targets(arguments: list[str], folders: list[Path]) -> list[Path] | None:
    if not arguments:
        return [Path.home()]
    if len(arguments) != 1 or arguments[0].startswith("-") or _GLOB.search(arguments[0]):
        return None
    targets: list[Path] = []
    for folder in folders:
        paths = _candidates(arguments[0], folder)
        if paths is None:
            return None
        targets.extend(paths)
    return targets


def _program_path(program: str) -> str | None:
    """The file a program runs from, as the allowlist stores it; ``None`` if it cannot be remembered."""
    if "=" in program or ("/" in program and not os.path.isabs(program)):
        return None
    name = Path(program).name
    if name in _RUNS_ANYTHING or _INTERPRETER.match(name):
        return None
    from flowly.exec.safety import resolve_executable

    return resolve_executable(program)


def _judge(command: str, cwd: str | None, remembered: Remembered | None) -> tuple[bool, list[str] | None]:
    """Whether the command runs unasked, and what "Always allow" would remember.

    The second value is ``None`` when remembering cannot make it run unasked.
    """
    segments = _segments(command)
    if segments is None:
        return False, None
    start = Path(cwd) if cwd else Path.home()
    granted = _granted(start)
    # After a cd the rest of the chain may run in either folder (``cd x || ls``).
    folders = [start]
    runs = True
    remember: list[str] | None = []
    for tokens, inputs in segments:
        if tokens[0] == "cd":
            targets = _cd_targets(tokens[1:], folders)
            if targets is None:
                runs, remember = False, None
            else:
                folders += [target for target in targets if target not in folders]
            continue
        if _reads_ok(inputs, folders, granted, False) and _segment_reads(tokens, folders, granted):
            continue
        walks = _walks(tokens[0], tokens[1:])
        paths = _path_arguments(tokens[1:], folders)
        if walks and not paths:
            paths.append(".")
        path = None if _reaches_protected(paths + inputs, folders, walks) else _program_path(tokens[0])
        if path is None:
            runs, remember = False, None
            continue
        if remembered is None or not remembered(path):
            runs = False
        if remember is not None and path not in remember:
            remember.append(path)
    return runs, remember


def runs_without_asking(command: str, cwd: str | None, remembered: Remembered | None = None) -> bool:
    """Whether Auto may run ``command`` in ``cwd`` (home when ``None``) without asking.

    ``remembered`` says whether the owner always allows a program, by the
    path it runs from; without it only reads run.
    """
    return _judge(command, cwd, remembered)[0]


def programs_to_remember(command: str, cwd: str | None) -> list[str]:
    """What "Always allow" remembers so that ``command`` runs unasked next time.

    Empty when remembering cannot do that: a part asks for a reason a program
    does not cover (a variable, a write redirection, a script run by a
    relative path, a shell or interpreter, a protected path), or every part
    already reads.
    """
    return _judge(command, cwd, None)[1] or []
