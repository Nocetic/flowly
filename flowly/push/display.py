"""Text that is safe to show in a phone notification.

A notification reads like a message from the user's agent, so it carries the
real content: the question, the command, the result. That text passes through
the relay, Apple's and Google's push services, and the lock screen, so every
title and body goes through :func:`safe_text` first:

1. Invisible and spoofing characters (zero-width, bidirectional overrides,
   control characters) are removed before anything else, so a secret split
   by one is still recognised; unusual spaces become spaces in prose and are
   removed from commands. :func:`has_hidden_characters` tells a caller to
   show a command holding any of them generically instead.
2. Credentials are redacted on the full text, before it is shortened, so a
   cut never exposes the start of a secret that the full pattern would have
   matched.
3. Whitespace is collapsed to one line (a command's line breaks become ``↵``)
   and the result is bounded.

See ``docs/engineering/notification-policy.md``.
"""

from __future__ import annotations

import re
import unicodedata

from flowly.compaction.redaction import REDACTED, redact_secrets

#: Text past this point can never be displayed (bodies are a few hundred
#: characters), so it is not scanned: a bound on the work done per push.
_MAX_SCAN = 8192

# Zero-width and bidi format characters, line/paragraph separators and every
# space that is not U+0020. Control characters are handled by category below.
_HIDDEN = re.compile(
    "[\u00ad\u061c\u115f\u1160\u180e\u200b-\u200f\u202a-\u202e\u2060-\u206f"
    "\u3164\ufe00-\ufe0f\ufeff\uffa0\U000e0000-\U000e007f]"
)
_ODD_SPACE = re.compile("[\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]")

# Shapes the conversation redactor does not cover because they only show up
# in commands. Each keeps what makes the text readable (the variable, the
# flag, the user) and drops the value.
_COMMAND_SECRETS: tuple[tuple[re.Pattern[str], str], ...] = (
    # A private key whose end is not in sight: everything after the header.
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*", re.DOTALL), f"{REDACTED} (private key)"),
    # Environment-style assignments: API_KEY=…, GITHUB_TOKEN="…", DB_PASSWORD='…'.
    (
        re.compile(
            r"\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|AUTH|CREDENTIALS?)[A-Z0-9_]*=)"
            r"(?:\"[^\"]*\"|'[^']*'|\S+)"
        ),
        rf"\1{REDACTED}",
    ),
    # Flags that take a credential: --password x, --token=x, --api-key "x".
    (
        re.compile(
            r"(?<![\w-])(--?(?:password|passwd|pass|token|api[-_]?key|secret|auth[-_]?token|"
            r"access[-_]?token|client[-_]?secret|private[-_]?key)(?:=|\s+))(?:\"[^\"]*\"|'[^']*'|\S+)",
            re.IGNORECASE,
        ),
        rf"\1{REDACTED}",
    ),
    # Webhook URLs whose path is the secret.
    (re.compile(r"(hooks\.slack\.com/services/)\S+"), rf"\1{REDACTED}"),
    (re.compile(r"(discord(?:app)?\.com/api/webhooks/\d+/)\S+"), rf"\1{REDACTED}"),
    # curl -u user:password, --user user:password: the user stays.
    (re.compile(r"((?:^|\s)(?:-u|--user)\s*[\"']?[^\s:\"']+:)[^\s\"']+"), rf"\1{REDACTED}"),
    # mysql -pSecret (the password glued to the flag), sshpass -p secret.
    (re.compile(r"(\b(?:mysql|mysqldump|mariadb|mysqladmin)\b[^|;&\n]*?\s-p)(?=\S)\S+"), rf"\1{REDACTED}"),
    (re.compile(r"(\bsshpass\s+-p\s*)\S+"), rf"\1{REDACTED}"),
    # Long random-looking tokens with no known prefix: 32+ characters mixing
    # upper case, lower case and digits, alone or as a URL path segment (a
    # webhook secret). Words and hex digests (commit ids, checksums) do not
    # qualify.
    (
        re.compile(
            r"(?<![A-Za-z0-9_+=-])(?=[A-Za-z0-9_+=-]*[a-z])(?=[A-Za-z0-9_+=-]*[A-Z])"
            r"(?=[A-Za-z0-9_+=-]*[0-9])[A-Za-z0-9_+=-]{32,}(?![A-Za-z0-9_+=-])"
        ),
        REDACTED,
    ),
)


def _is_control(char: str) -> bool:
    return unicodedata.category(char) in ("Cc", "Cs", "Co", "Cn") and char not in "\n\t"


def has_hidden_characters(text: str) -> bool:
    """True when ``text`` holds characters a reader cannot see or tell apart.

    Unusual spaces count: in a command they can split a secret past the
    redactor, or make two different commands look the same.
    """
    return (
        bool(_HIDDEN.search(text))
        or bool(_ODD_SPACE.search(text))
        or any(_is_control(char) for char in text if char != "\r")
    )


def redact(text: str) -> str:
    """Credentials in ``text`` replaced; everything else as it was."""
    redacted = redact_secrets(text)
    for pattern, replacement in _COMMAND_SECRETS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def safe_text(text: object, limit: int, *, command: bool = False) -> str:
    """``text`` cleaned, redacted, on one line and at most ``limit`` characters."""
    value = str(text or "")[:_MAX_SCAN].replace("\r\n", "\n").replace("\r", "\n")
    value = _HIDDEN.sub("", value)
    # In a command an unusual space is removed, so a secret split by one is
    # whole again for the redactor; in prose it is just a space.
    value = _ODD_SPACE.sub("" if command else " ", value)
    value = "".join(char for char in value if not _is_control(char))
    value = redact(value)
    if command:
        value = " ↵ ".join(part.strip() for part in value.split("\n") if part.strip())
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > limit:
        value = value[: max(0, limit - 1)].rstrip() + "…"
    return value
