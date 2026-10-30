"""Which names a secret may have: the environment variable a command reads it as.

A secret is handed to the agent's commands as an environment variable, so its
name is one. Refused are the names that would change how a command or the
agent itself runs (``PATH``, the dynamic loader's and interpreters' variables,
shell start-up hooks) and the agent's own credentials (``FLOWLY_*``), which a
secret must never shadow.

A provider key the person saves for the agent to try (``OPENROUTER_API_KEY``)
is allowed, although ``flowly.exec.env_scrub`` strips that name from a
command's inherited environment: the scrub keeps Flowly's own configured keys
out of commands, and a saved secret is handed over on purpose
(``flowly.secrets.service.command_env``).
"""

from __future__ import annotations

import re

NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

_RESERVED = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD", "OLDPWD", "TMPDIR", "TMP", "TEMP",
    "LANG", "TERM", "TZ", "IFS", "PS1", "PS2", "PS4", "ENV", "BASH_ENV", "PROMPT_COMMAND",
    "CDPATH", "GLOBIGNORE", "SHELLOPTS", "BASHOPTS", "HISTFILE", "ZDOTDIR",
    "NODE_OPTIONS", "NODE_PATH", "PERL5LIB", "PERL5OPT", "PERLLIB", "RUBYOPT", "RUBYLIB",
    "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "CLASSPATH",
    "GIT_SSH", "GIT_SSH_COMMAND", "GIT_EXEC_PATH", "GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_ASKPASS", "SSH_ASKPASS", "SSH_AUTH_SOCK",
    "EDITOR", "VISUAL", "PAGER", "BROWSER", "DISPLAY", "XDG_RUNTIME_DIR",
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "PSMODULEPATH",
    "MOLTBOT_PROXY_JWT_SECRET",
})
_RESERVED_PREFIXES = ("LC_", "LD_", "DYLD_", "PYTHON", "FLOWLY_", "__FLOWLY", "NPM_CONFIG_", "UV_")


class SecretNameError(ValueError):
    """A name no secret may have; ``str()`` says why, for the agent and the person."""


def check_name(name: object) -> str:
    """The name, if a secret may have it; else :class:`SecretNameError`."""
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise SecretNameError(
            "A secret's name is an environment variable name: capital letters, digits and "
            "underscores, starting with a letter, 2 to 64 characters (OPENROUTER_API_KEY)."
        )
    if name in _RESERVED or name.startswith(_RESERVED_PREFIXES):
        raise SecretNameError(f"{name} changes how commands or the agent run; choose another name.")
    return name
