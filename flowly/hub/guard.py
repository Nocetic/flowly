"""The gate every skill passes before it lands in an agent's skills folder.

A skill is markdown the agent follows plus whatever scripts ride along, so a
bad one is an instruction to the agent, not just a file. ``SkillManager``
downloads into a staging folder outside the skills tree, this module reads
what arrived, and only a skill the policy lets through is moved into place.

Trust comes from where the skill came from:

- ``builtin``   — shipped with Flowly (never installed through here, listed for
  completeness of the policy).
- ``trusted``   — the Flowly marketplace registry, which scans at publish time
  and serves a hash the download is checked against.
- ``community`` — anything else: a GitHub path, a raw URL, a local folder.

The verdict is the worst finding: any critical or high finding is
``dangerous``, any medium one ``caution``, otherwise ``safe`` (low findings are
reported but never block). The owner can accept a community skill with a
cautionary finding (``accept_caution``, the CLI only); nothing lets a dangerous
one in from the registry or the community.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

TRUST_BUILTIN = "builtin"
TRUST_REGISTRY = "trusted"
TRUST_COMMUNITY = "community"

SAFE, CAUTION, DANGEROUS = "safe", "caution", "dangerous"

#               safe     caution  dangerous
POLICY: dict[str, tuple[bool, bool, bool]] = {
    TRUST_BUILTIN: (True, True, True),
    TRUST_REGISTRY: (True, True, False),
    TRUST_COMMUNITY: (True, False, False),
}
_VERDICT_COLUMN = {SAFE: 0, CAUTION: 1, DANGEROUS: 2}

MAX_FILES = 200
MAX_TOTAL_BYTES = 5 * 1024 * 1024
MAX_SCANNED_FILE_BYTES = 1024 * 1024

_SKILL_DIR_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

_TEXT_SUFFIXES = {
    "", ".md", ".txt", ".py", ".sh", ".bash", ".zsh", ".fish", ".js", ".mjs", ".cjs", ".ts",
    ".rb", ".pl", ".php", ".ps1", ".bat", ".cmd", ".json", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".html", ".xml", ".csv", ".sql", ".applescript", ".scpt",
}
_BINARY_SUFFIXES = {".exe", ".dll", ".so", ".dylib", ".bin", ".app", ".pkg", ".dmg", ".msi"}
_EXECUTABLE_MAGIC = (b"\x7fELF", b"MZ", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe")

# (rule id, severity, category, what it means, pattern). Patterns run per line,
# case-insensitively, over every text file in the skill.
_RULES: tuple[tuple[str, str, str, str, str], ...] = (
    ("reverse-shell-devtcp", "critical", "remote-access", "opens a shell over /dev/tcp",
     r"/dev/(tcp|udp)/"),
    ("reverse-shell-netcat", "critical", "remote-access", "hands a shell to netcat",
     r"\b(nc|ncat|netcat)\b[^\n]*\s-(e|c)\s"),
    ("interactive-shell-redirect", "critical", "remote-access", "redirects an interactive shell",
     r"\b(ba|z)?sh\s+-i\s*[<>]&"),
    ("env-exfiltration", "critical", "exfiltration", "sends the environment over the network",
     r"\b(env|printenv|set)\b\s*\|\s*(curl|wget|nc|ncat)\b"),
    ("wipe-home-or-root", "critical", "destructive", "deletes the home folder or the disk root",
     r"\brm\s+-[a-z]*r[a-z]*\s+(--no-preserve-root\s+)?(/|~/?|\$HOME/?|\$\{HOME\}/?)(\s|$|;|&)"),
    ("format-disk", "critical", "destructive", "formats or overwrites a disk",
     r"\bmkfs(\.[a-z0-9]+)?\b|\bdd\b[^\n]*\bof=/dev/(sd|hd|disk|nvme|rdisk)"),
    ("fork-bomb", "critical", "destructive", "a fork bomb",
     r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
    ("secret-files", "medium", "secret-access", "reads keys or credentials stored on the machine",
     r"~?/?\.ssh/id_|\.aws/credentials|\.config/gcloud|\.netrc\b|\.docker/config\.json"),
    ("keychain-read", "medium", "secret-access", "reads passwords from the keychain",
     r"\bsecurity\s+(find|dump)-(generic|internet)-password|\bsecurity\s+dump-keychain"),
    ("startup-persistence", "high", "persistence", "installs itself to run at startup or on a schedule",
     r"\|\s*crontab\b|\bcrontab\s+(?!-)[\w./~$]|\blaunchctl\s+(load|bootstrap|submit)\b|"
     r">>?\s*~?/?\.(bashrc|zshrc|bash_profile|profile|zprofile)\b|authorized_keys"),
    ("decode-and-run", "high", "obfuscation", "decodes hidden code and runs it",
     r"base64\s+(-d|-D|--decode)\b[^\n]*\|\s*(sudo\s+)?(ba|z)?sh\b|"
     r"\b(eval|exec)\s*\(\s*[^\n)]*(b64decode|atob|base64|fromCharCode|decode\(['\"]hex)"),
    ("override-instructions", "high", "prompt-injection", "tells the agent to drop its instructions",
     r"\b(ignore|disregard|forget|override)\s+(all\s+|any\s+)?(of\s+)?(your\s+|the\s+)?"
     r"(previous|prior|above|earlier|system|original)\s+(instructions|prompts?|rules|guidelines)"),
    ("hide-from-owner", "medium", "prompt-injection", "tells the agent to keep something from its owner",
     r"\b(do\s+not|don'?t|never)\s+(tell|inform|show|mention\s+(this|it)\s+to|alert|notify)\s+the\s+"
     r"(user|owner)\b|\bwithout\s+(telling|asking|informing|notifying)\s+the\s+(user|owner)\b"),
    ("flowly-secrets", "medium", "secret-access", "reads Flowly's own settings or saved keys",
     r"\.flowly/[^\s'\"]*(config\.json|secrets|credentials|tokens?)\b"),
    ("pipe-to-shell", "medium", "remote-code", "runs a script straight from the internet",
     r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b|\biwr\b[^\n|]*\|\s*iex\b"),
    ("invisible-text", "medium", "obfuscation", "contains invisible or direction-changing characters",
     "[​-‏‪-‮⁠-⁤⁦-⁩﻿]"),
    ("disable-safety", "medium", "prompt-injection", "asks the agent to skip approvals or safety checks",
     r"\b(skip|bypass|disable|turn\s+off)\s+(the\s+)?(approval|confirmation|safety|security)\s*"
     r"(checks?|prompts?|guard)?\b|--dangerously-skip-permissions|\byolo\s+mode\b"),
    ("sudo", "low", "privilege", "runs a command as administrator",
     r"\bsudo\s+\S"),
)
_COMPILED = tuple(
    (rule, severity, category, meaning, re.compile(pattern, re.IGNORECASE))
    for rule, severity, category, meaning, pattern in _RULES
)


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    category: str
    meaning: str
    file: str
    line: int
    excerpt: str

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "category": self.category,
            "meaning": self.meaning,
            "file": self.file,
            "line": self.line,
            "excerpt": self.excerpt,
        }


@dataclass
class ScanResult:
    name: str
    trust: str
    verdict: str
    findings: list[Finding] = field(default_factory=list)

    def summary(self) -> dict:
        """The scan as stored next to the skill and in the audit log."""
        return {
            "trust": self.trust,
            "verdict": self.verdict,
            "findings": [finding.to_dict() for finding in self.findings],
        }


class SkillBlocked(Exception):
    """The policy refused a skill; nothing was installed."""

    code = "SKILL_BLOCKED"

    def __init__(self, result: ScanResult, reason: str):
        super().__init__(reason)
        self.result = result
        self.reason = reason


def trust_for(source_type: str) -> str:
    """The trust a parsed source carries (``HubClient.parse_skill_source``)."""
    return TRUST_REGISTRY if source_type == "registry" else TRUST_COMMUNITY


def valid_skill_dir_name(name: str) -> bool:
    """A name that can only ever be one folder directly under the skills folder."""
    return bool(_SKILL_DIR_NAME.match(name)) and ".." not in name


def scan(skill_dir: Path, trust: str) -> ScanResult:
    """Read every file of a staged skill and judge it."""
    findings: list[Finding] = []
    root = skill_dir.resolve()
    files: list[Path] = []
    total = 0
    for path in sorted(skill_dir.rglob("*")):
        relative = path.relative_to(skill_dir).as_posix()
        if path.is_symlink():
            findings.append(Finding("symlink", "critical", "structure",
                                    "links to a file outside the skill", relative, 0, ""))
            continue
        if not path.is_file():
            continue
        if not path.resolve().is_relative_to(root):
            findings.append(Finding("escapes-folder", "critical", "structure",
                                    "a file outside the skill folder", relative, 0, ""))
            continue
        files.append(path)
        total += path.stat().st_size
    if len(files) > MAX_FILES:
        findings.append(Finding("too-many-files", "high", "structure",
                                f"more than {MAX_FILES} files", ".", 0, str(len(files))))
    if total > MAX_TOTAL_BYTES:
        findings.append(Finding("too-large", "high", "structure",
                                "larger than a skill should be", ".", 0, f"{total} bytes"))
    for path in files[:MAX_FILES]:
        findings.extend(_scan_file(path, path.relative_to(skill_dir).as_posix()))
    return ScanResult(skill_dir.name, trust, _verdict(findings), findings)


def decide(result: ScanResult, accept_caution: bool = False) -> tuple[bool, str]:
    """``(allowed, reason)`` for a scanned skill under the trust policy."""
    allowed = POLICY.get(result.trust, POLICY[TRUST_COMMUNITY])[_VERDICT_COLUMN[result.verdict]]
    count = len(result.findings)
    if allowed:
        return True, f"Allowed: {result.trust} source, {result.verdict} ({count} findings)."
    if accept_caution and result.verdict != DANGEROUS:
        return True, f"Accepted by the owner despite a {result.verdict} verdict ({count} findings)."
    reason = f"Blocked: {result.trust} source, {result.verdict} ({count} findings)."
    if result.verdict == DANGEROUS:
        return False, reason + " A dangerous skill is never installed."
    return False, reason + " The owner can accept it from the command line."


def _verdict(findings: list[Finding]) -> str:
    severities = {finding.severity for finding in findings}
    if severities & {"critical", "high"}:
        return DANGEROUS
    if "medium" in severities:
        return CAUTION
    return SAFE


def _scan_file(path: Path, relative: str) -> list[Finding]:
    suffix = path.suffix.lower()
    try:
        with path.open("rb") as handle:
            head = handle.read(4)
    except OSError:
        return [Finding("unreadable", "high", "structure", "a file that cannot be read", relative, 0, "")]
    if suffix in _BINARY_SUFFIXES or head.startswith(_EXECUTABLE_MAGIC):
        return [Finding("binary-executable", "high", "structure", "ships a compiled program", relative, 0, "")]
    if suffix not in _TEXT_SUFFIXES:
        # Images, fonts and other assets are carried, not read by the agent.
        return []
    if path.stat().st_size > MAX_SCANNED_FILE_BYTES:
        return [Finding("file-too-large", "medium", "structure",
                        "a text file too large to read through", relative, 0, "")]
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return [Finding("not-text", "medium", "structure", "a text file that is not text", relative, 0, "")]
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for rule, severity, category, meaning, pattern in _COMPILED:
            match = pattern.search(line)
            if match:
                excerpt = line.strip()[:120]
                findings.append(Finding(rule, severity, category, meaning, relative, number, excerpt))
    return findings
