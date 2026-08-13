"""Redact credentials on their way into a compaction summary.

A summary is a persistence boundary, not a transient view: it is written to
disk, re-injected at the head of every later prompt, and folded into the next
summary. A credential that lands in one therefore stops being a fact about a
single turn and becomes a permanent resident of the conversation — and of the
session file on disk.

The summarizer prompt asks the model not to reproduce secrets. This module
exists because asking is not a guarantee: the model can ignore the
instruction, and the transcript it reads may contain a credential in a shape
it does not recognise as one.

Scope is deliberately narrow. Only shapes we can match with high confidence
are redacted, because over-redaction quietly deletes context the summary
existed to preserve, and that failure is invisible. LIVE tool output is a
different trade-off entirely — a user debugging an auth flow needs to see the
token — and is not touched here.
"""

import re

REDACTED = "[redacted]"

# Each pattern must be specific enough that a false positive is implausible in
# ordinary prose. Where a pattern has a "keep this part" prefix (a scheme, a
# header name), it is captured so the summary still says WHAT was there.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # PEM private key blocks — replaced whole, header included: the header
    # alone tells the reader what was removed.
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        f"{REDACTED} (private key)",
    ),
    # Authorization headers, any scheme.
    (
        re.compile(r"\b(Authorization\s*:\s*)(\S+\s+)?\S+", re.IGNORECASE),
        rf"\1\2{REDACTED}",
    ),
    (re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), rf"\1{REDACTED}"),
    # Credentials embedded in a URL's userinfo. The host is kept — knowing
    # which host was reached is usually the point of the sentence.
    (
        re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@"),
        rf"\1{REDACTED}@",
    ),
    # Provider key shapes with a distinctive prefix.
    (re.compile(r"\bsk-[A-Za-z0-9._-]{16,}"), REDACTED),          # OpenAI-style
    (re.compile(r"\bsk-ant-[A-Za-z0-9._-]{16,}"), REDACTED),      # Anthropic
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), REDACTED),      # GitHub
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), REDACTED),   # Slack
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),              # AWS access key
    # Google API key. The ``AIza`` prefix is what makes this unambiguous, so
    # the length is a lower bound rather than the exact 35 — key formats drift
    # and a missed credential costs more than a slightly looser match.
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), REDACTED),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"), REDACTED),        # GitLab
    (re.compile(r"\bnpm_[A-Za-z0-9]{30,}"), REDACTED),
    (re.compile(r"\bdop_v1_[A-Za-z0-9]{32,}"), REDACTED),         # DigitalOcean
    # JSON Web Tokens — three base64url segments. Long enough that prose
    # cannot collide with it.
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        REDACTED,
    ),
    # "<secret-ish name> = <value>" / ": <value>" assignments. The NAME is
    # kept so the summary can still say a token was configured.
    (
        re.compile(
            r"\b((?:api[_-]?key|apikey|secret|password|passwd|token|access[_-]?token|"
            r"refresh[_-]?token|client[_-]?secret|private[_-]?key)"
            r"\s*[:=]\s*)(?![\s\"']*(?:$|\n))[\"']?[^\s\"',;]{8,}[\"']?",
            re.IGNORECASE,
        ),
        rf"\1{REDACTED}",
    ),
)


def redact_secrets(text: str) -> str:
    """Return ``text`` with recognisable credentials replaced.

    Structure is preserved wherever it carries meaning — the URL's host, the
    header's name, the setting's key — so the summary still records that a
    credential was involved without carrying the credential itself.
    """
    if not text:
        return text
    redacted = text
    for pattern, replacement in _PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted
